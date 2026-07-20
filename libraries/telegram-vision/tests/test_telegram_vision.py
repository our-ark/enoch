from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from our_ark_telegram_vision import (
    MAX_TELEGRAM_IMAGE_BYTES,
    TelegramImage,
    TelegramVisionError,
    select_telegram_image,
    telegram_image_prompt,
    temporary_telegram_image,
)


class TelegramVisionLibraryTests(unittest.TestCase):
    def test_selects_largest_photo_and_supported_document(self) -> None:
        photo = select_telegram_image(
            {
                "photo": [
                    {"file_id": "small", "width": 90, "height": 90, "file_size": 100},
                    {"file_id": "large", "width": 1280, "height": 720, "file_size": 500},
                ]
            }
        )
        document = select_telegram_image(
            {
                "document": {
                    "file_id": "png-file",
                    "mime_type": "image/png",
                    "file_size": 1234,
                }
            }
        )

        self.assertEqual(photo, TelegramImage("large", ".jpg", 500))
        self.assertEqual(document, TelegramImage("png-file", ".png", 1234))

    def test_rejects_non_image_document(self) -> None:
        self.assertIsNone(
            select_telegram_image(
                {"document": {"file_id": "pdf-file", "mime_type": "application/pdf"}}
            )
        )

    def test_temporary_image_is_private_and_deleted(self) -> None:
        client = FakeDownloader(b"\xff\xd8\xfftelegram-photo")
        with TemporaryDirectory() as temp:
            directory = Path(temp) / "images"
            with temporary_telegram_image(
                client,
                TelegramImage(file_id="photo", suffix=".jpg"),
                directory,
            ) as path:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                saved_path = path

            self.assertFalse(saved_path.exists())
        self.assertEqual(client.calls, [("photo", MAX_TELEGRAM_IMAGE_BYTES)])

    def test_invalid_download_is_deleted_and_rejected(self) -> None:
        client = FakeDownloader(b"not an image")
        with TemporaryDirectory() as temp:
            directory = Path(temp) / "images"
            with self.assertRaisesRegex(TelegramVisionError, "unsupported or invalid"):
                with temporary_telegram_image(
                    client,
                    TelegramImage(file_id="photo", suffix=".jpg"),
                    directory,
                ):
                    pass
            self.assertEqual(list(directory.iterdir()), [])

    def test_prompt_keeps_image_instructions_untrusted_and_read_only(self) -> None:
        prompt = telegram_image_prompt("这是什么花？")

        self.assertIn("这是什么花？", prompt)
        self.assertIn("untrusted image content", prompt)
        self.assertIn("read-only", prompt)


class FakeDownloader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[str, int]] = []

    def download_file(self, file_id: str, destination: Path, *, max_bytes: int) -> None:
        self.calls.append((file_id, max_bytes))
        destination.write_bytes(self.content)


if __name__ == "__main__":
    unittest.main()
