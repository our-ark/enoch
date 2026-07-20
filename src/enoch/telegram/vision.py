from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from enoch.paths import enoch_home
from enoch.runtime_dependencies import activate_runtime_dependencies
from enoch.telegram.client import TelegramError


activate_runtime_dependencies()

from our_ark_telegram_vision import (  # noqa: E402
    MAX_TELEGRAM_IMAGE_BYTES,
    TelegramFileDownloader,
    TelegramImage,
    TelegramVisionError,
    select_telegram_image,
    telegram_image_prompt,
    temporary_telegram_image as _temporary_telegram_image,
)


@contextmanager
def temporary_telegram_image(
    client: TelegramFileDownloader,
    image: TelegramImage,
    root: Path,
) -> Iterator[Path]:
    directory = enoch_home(root) / "telegram" / "images"
    try:
        with _temporary_telegram_image(client, image, directory) as path:
            yield path
    except TelegramVisionError as error:
        raise TelegramError(str(error)) from error
