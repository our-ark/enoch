from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.telegram.client import TelegramError
from enoch.telegram.vision import (
    MAX_TELEGRAM_IMAGE_BYTES,
    TelegramImage,
    select_telegram_image,
    telegram_image_prompt,
    temporary_telegram_image,
)


class TelegramVisionTests(unittest.TestCase):
    def test_selects_largest_telegram_photo_variant(self) -> None:
        image = select_telegram_image(
            {
                "photo": [
                    {"file_id": "small", "width": 90, "height": 90, "file_size": 100},
                    {"file_id": "large", "width": 1280, "height": 720, "file_size": 500},
                ]
            }
        )

        self.assertEqual(image, TelegramImage(file_id="large", suffix=".jpg", file_size=500))

    def test_accepts_supported_image_document(self) -> None:
        image = select_telegram_image(
            {
                "document": {
                    "file_id": "png-file",
                    "mime_type": "image/png",
                    "file_size": 1234,
                }
            }
        )

        self.assertEqual(image, TelegramImage(file_id="png-file", suffix=".png", file_size=1234))

    def test_rejects_non_image_document(self) -> None:
        self.assertIsNone(
            select_telegram_image(
                {"document": {"file_id": "pdf-file", "mime_type": "application/pdf"}}
            )
        )

    def test_temporary_image_is_private_and_deleted_after_use(self) -> None:
        client = FakeDownloader(b"\xff\xd8\xfftelegram-photo")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with temporary_telegram_image(
                client,
                TelegramImage(file_id="photo", suffix=".jpg"),
                root,
            ) as path:
                self.assertTrue(path.exists())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                saved_path = path

            self.assertFalse(saved_path.exists())

        self.assertEqual(client.calls, [("photo", MAX_TELEGRAM_IMAGE_BYTES)])

    def test_invalid_download_is_deleted_and_rejected(self) -> None:
        client = FakeDownloader(b"not an image")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(TelegramError, "unsupported or invalid"):
                with temporary_telegram_image(
                    client,
                    TelegramImage(file_id="photo", suffix=".jpg"),
                    root,
                ):
                    pass

            image_dir = root / ".enoch" / "telegram" / "images"
            self.assertEqual(list(image_dir.iterdir()), [])

    def test_image_prompt_separates_caption_from_untrusted_image_text(self) -> None:
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
