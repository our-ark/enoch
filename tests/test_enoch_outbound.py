from __future__ import annotations

import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "slack" / "src"))

from enoch.outbound import (
    OutboundArtifactError,
    artifact_relative_path,
    capture_runtime_attachments,
    import_outbound_artifact,
)
from enoch.app.inbox import begin_event, complete_event
from enoch.paths import storage_layout
from enoch.providers import (
    ChatEvent,
    NotificationIntent,
    RuntimeEvent,
    RuntimeOutputReference,
    RuntimeResult,
)
from our_ark_slack import SlackClient, SlackConfig


class OutboundArtifactTests(unittest.TestCase):
    def test_artifact_uri_rejects_noncanonical_aliases(self) -> None:
        for uri in (
            "artifact://outbound//file.txt",
            "artifact://outbound/./file.txt",
            "artifact://outbound/../file.txt",
        ):
            with self.subTest(uri=uri), self.assertRaises(OutboundArtifactError):
                artifact_relative_path(uri)

    def test_image_generation_event_is_imported_and_deduplicated(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "agent"
            generated = base / "codex" / "generated_images" / "run-1"
            generated.mkdir(parents=True)
            image = generated / "avatar.png"
            image.write_bytes(_png_bytes())
            result = RuntimeResult(
                final_text="Here is the avatar.",
                events=(
                    RuntimeEvent(
                        "item.completed",
                        {"item": {
                            "type": "Extension",
                            "kind": "image_gen.generation",
                            "savedPath": str(image),
                        }},
                    ),
                    RuntimeEvent(
                        "item.completed",
                        {"item": {"type": "ImageView", "path": image.as_uri()}},
                    ),
                ),
            )

            with patch.dict(os.environ, {"CODEX_HOME": str(base / "codex")}):
                capture = capture_runtime_attachments(result, root)

            self.assertEqual(capture.rejected, 0)
            self.assertEqual(len(capture.attachments), 1)
            attachment = capture.attachments[0]
            self.assertEqual(attachment.filename, "avatar.png")
            self.assertEqual(attachment.mime_type, "image/png")
            self.assertEqual(attachment.kind, "image")
            relative = Path(attachment.uri.removeprefix("artifact://"))
            imported = storage_layout(root).artifacts / relative
            self.assertEqual(imported.read_bytes(), image.read_bytes())
            self.assertEqual(imported.stat().st_mode & 0o777, 0o600)

    def test_runtime_image_reaches_slack_external_upload_request(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "agent"
            generated = base / "codex" / "generated_images" / "session"
            generated.mkdir(parents=True)
            image = generated / "avatar.png"
            image.write_bytes(_png_bytes())
            result = RuntimeResult(
                final_text="Here is the avatar.",
                events=(RuntimeEvent(
                    "item_completed",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "Extension",
                            "kind": "image_gen.generation",
                            "savedPath": str(image),
                        },
                    },
                ),),
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(base / "codex")}):
                capture = capture_runtime_attachments(result, root)

            web = _SlackWeb()
            uploads = []
            client = SlackClient(
                SlackConfig("xoxb-test", "xapp-test", "D123", "U123"),
                root / ".enoch" / "channels" / "slack" / "intake",
                web_client=web,
                approved_artifact_roots=(storage_layout(root).artifacts,),
                binary_uploader=lambda url, data, mime: uploads.append(
                    (url, data, mime)
                ),
            )

            receipt = client.deliver_notification(NotificationIntent(
                idempotency_key="runtime-avatar",
                operation="send",
                conversation_id="D123",
                thread_id="1700.100",
                text=result.final_text,
                attachments=capture.attachments,
            ))

        self.assertEqual(receipt.status, "delivered")
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][1], _png_bytes())
        self.assertEqual(uploads[0][2], "image/png")
        self.assertEqual(
            [name for name, _kwargs in web.calls],
            ["files.getUploadURLExternal", "files.completeUploadExternal"],
        )
        completion = web.calls[-1][1]
        self.assertEqual(completion["channel_id"], "D123")
        self.assertEqual(completion["thread_ts"], "1700.100")
        self.assertEqual(completion["initial_comment"], "Here is the avatar.")

    def test_explicit_artifact_reference_is_supported(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            artifact = storage_layout(root).artifacts / "reports" / "summary.txt"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("safe report", encoding="utf-8")
            result = RuntimeResult(
                final_text="report",
                output_refs=(
                    RuntimeOutputReference("artifact", "artifact://reports/summary.txt"),
                ),
            )

            capture = capture_runtime_attachments(result, root)

            self.assertEqual(capture.rejected, 0)
            self.assertEqual(capture.attachments[0].mime_type, "text/plain")

    def test_common_image_types_are_verified(self) -> None:
        samples = {
            "image.png": _png_bytes(),
            "image.jpg": b"\xff\xd8\xff\xe0safe\xff\xd9",
            "image.webp": b"RIFF\x04\x00\x00\x00WEBP",
            "image.gif": b"GIF89a\x01\x00\x01\x00;",
        }
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "agent"
            generated = base / "codex" / "generated_images"
            generated.mkdir(parents=True)
            with patch.dict(os.environ, {"CODEX_HOME": str(base / "codex")}):
                for filename, content in samples.items():
                    with self.subTest(filename=filename):
                        source = generated / filename
                        source.write_bytes(content)
                        attachment = import_outbound_artifact(source, root)
                        self.assertEqual(attachment.filename, filename)
                        self.assertTrue(attachment.mime_type.startswith("image/"))

    def test_rejects_outside_paths_traversal_symlinks_and_missing_files(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "agent"
            codex = base / "codex"
            generated = codex / "generated_images"
            generated.mkdir(parents=True)
            outside = base / "secret.txt"
            outside.write_text("secret", encoding="utf-8")
            link = generated / "linked.txt"
            link.symlink_to(outside)
            hardlink = generated / "hardlinked.txt"
            os.link(outside, hardlink)
            traversal = generated / ".." / "outside.txt"
            traversal.write_text("outside", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
                for source in (
                    outside,
                    link,
                    hardlink,
                    traversal,
                    generated / "missing.png",
                ):
                    with self.subTest(source=source), self.assertRaises(OutboundArtifactError):
                        import_outbound_artifact(source, root)

    def test_rejects_mime_mismatch_size_hidden_and_sensitive_content(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "agent"
            codex = base / "codex"
            generated = codex / "generated_images"
            generated.mkdir(parents=True)
            samples = {
                "wrong.png": b"plain text",
                ".hidden.txt": b"hidden",
                "private.pem": b"-----BEGIN PRIVATE KEY-----\nsecret",
                "token.txt": b"xoxb-secret-value",
                "large.txt": b"12345",
            }
            for filename, content in samples.items():
                (generated / filename).write_bytes(content)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
                for filename in samples:
                    limit = 4 if filename == "large.txt" else None
                    with self.subTest(filename=filename), self.assertRaises(OutboundArtifactError):
                        import_outbound_artifact(
                            generated / filename,
                            root,
                            max_bytes=limit,
                        )

    def test_untrusted_runtime_reference_is_counted_without_exposing_the_path(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "agent"
            outside = base / "credentials.txt"
            outside.write_text("do not send", encoding="utf-8")
            result = RuntimeResult(
                final_text="done",
                output_refs=(RuntimeOutputReference("file", outside.as_uri()),),
            )

            capture = capture_runtime_attachments(result, root)

            self.assertEqual(capture.attachments, ())
            self.assertEqual(capture.rejected, 1)

    def test_inbox_receipt_restores_verified_attachments(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = storage_layout(root).artifacts / "source" / "note.txt"
            source.parent.mkdir(parents=True)
            source.write_text("safe note", encoding="utf-8")
            attachment = import_outbound_artifact(source, root)
            event = ChatEvent(
                cursor=1,
                conversation_id="room-1",
                message_id="message-1",
                thread_id="parent-1",
                text="create a note",
            )
            started = begin_event("test", event, root)
            complete_event(
                "test",
                started.key,
                root,
                reply="done",
                logged_input="create a note",
                attachments=(attachment,),
            )

            restored = begin_event("test", event, root)

        self.assertTrue(restored.completed)
        self.assertEqual(restored.attachments, (attachment,))


def _png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"\x00\xff\xff\xff\xff"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class _SlackWeb:
    def __init__(self) -> None:
        self.calls = []

    def files_getUploadURLExternal(self, **kwargs):
        self.calls.append(("files.getUploadURLExternal", kwargs))
        return {
            "ok": True,
            "file_id": "F123",
            "upload_url": "https://files.slack.com/upload/v1/F123",
        }

    def files_completeUploadExternal(self, **kwargs):
        self.calls.append(("files.completeUploadExternal", kwargs))
        return {"ok": True, "files": kwargs["files"]}


if __name__ == "__main__":
    unittest.main()
