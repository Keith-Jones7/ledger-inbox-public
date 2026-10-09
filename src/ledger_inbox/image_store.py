from __future__ import annotations

import hashlib
import io
import os
import tempfile
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()
TYPES = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
    "HEIF": ("heic", "image/heic"),
    "AVIF": ("avif", "image/avif"),
}


def detect_image_type(data: bytes, max_pixels: int = 25_000_000) -> tuple[str, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in TYPES:
                    raise ValueError("image format is not supported")
                if (
                    image.width * image.height > max_pixels
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError("image exceeds pixel limit or is animated")
                detected = TYPES[image.format]
                image.verify()
            # verify() alone does not fully decode every format.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
            return detected
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError(
            "image is damaged or not a supported PNG, JPEG, WebP, HEIC, or AVIF"
        ) from exc


def model_image(path: str) -> bytes:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1600, 1600))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def private_directory(path: Path) -> None:
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ImageStore:
    def __init__(self, root: Path, max_pixels: int = 25_000_000) -> None:
        self.root = root
        self.max_pixels = max_pixels

    def put(self, tenant_id: str, data: bytes) -> tuple[str, str, str]:
        extension, media_type = detect_image_type(data, self.max_pixels)
        digest = hashlib.sha256(data).hexdigest()
        destination = self.root / tenant_id / digest[:2] / f"{digest}.{extension}"
        private_directory(destination.parent)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError("stored image integrity check failed")
            return digest, media_type, str(destination)
        fd, temp_name = tempfile.mkstemp(prefix=".incoming-", dir=destination.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, destination)
            sync_directory(destination.parent)
        finally:
            temp_path.unlink(missing_ok=True)
        return digest, media_type, str(destination)
