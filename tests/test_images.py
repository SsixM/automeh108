from __future__ import annotations

import hashlib
from io import BytesIO

from PIL import Image

import bot


def make_photo(width: int = 120, height: int = 60, orientation: int | None = None) -> bot.PhotoInput:
    image = Image.new("RGB", (width, height), (200, 30, 30))
    output = BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    image.save(output, format="JPEG", quality=90, exif=exif)
    data = output.getvalue()
    return bot.PhotoInput(data, "image/jpeg", "photo.jpg", hashlib.sha256(data).hexdigest())


def test_exif_orientation_is_applied() -> None:
    # 6 = поворот на 90° по часовой стрелке: 120×60 превращается в 60×120.
    processed = bot.GeminiClient._image_bytes(make_photo(orientation=6))
    image = Image.open(BytesIO(processed))
    assert image.size == (60, 120)


def test_homework_image_strips_exif_and_limits_size() -> None:
    data, mime, extension, width, height = bot.GeminiClient.homework_image(
        make_photo(3200, 1800, orientation=1)
    )
    assert mime in {"image/webp", "image/jpeg"}
    assert extension in {"webp", "jpg"}
    assert max(width, height) <= bot.MAX_HOMEWORK_IMAGE_SIDE
    reopened = Image.open(BytesIO(data))
    assert not reopened.getexif()


def test_oversized_photo_is_rejected() -> None:
    photo = bot.PhotoInput(b"x" * (bot.MAX_IMAGE_BYTES + 1), "image/jpeg", "x.jpg", "a" * 64)
    try:
        bot.GeminiClient.homework_image(photo)
    except bot.UserFacingError as exc:
        assert "слишком" in str(exc)
    else:
        raise AssertionError("ожидалась ошибка размера")
