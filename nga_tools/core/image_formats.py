from __future__ import annotations

import importlib
import threading
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any

from PIL import Image

_PIL_OPENERS_LOCK = threading.Lock()
_pillow_openers_registered = False
_PIL_AVIF_SUPPORT_WARNING = (
    "image file could not be identified because AVIF support not installed"
)
_MODERN_IMAGE_CODECS = (
    ("avif", "avif_decode"),
    ("jxl", "jpegxl_decode"),
)
_HEADER_READ_SIZE = 128
_AVIF_BRANDS = {b"avif", b"avis"}
_JPEG_XL_CODESTREAM_MAGIC = b"\xff\x0a"
_JPEG_XL_CONTAINER_MAGIC = b"\x00\x00\x00\x0cJXL \r\n\x87\n"

IMAGE_FORMAT_BY_PILLOW_FORMAT = {
    "JPEG": "jpg",
    "PNG": "png",
    "GIF": "gif",
    "WEBP": "webp",
    "HEIF": "heif",
    "AVIF": "avif",
    "JPEGXL": "jxl",
}


def register_pillow_image_openers() -> None:
    global _pillow_openers_registered

    if _pillow_openers_registered:
        return

    with _PIL_OPENERS_LOCK:
        if _pillow_openers_registered:
            return
        try:
            pillow_heif = importlib.import_module("pillow_heif")
        except ImportError:
            _pillow_openers_registered = True
            return

        register_heif_opener = getattr(pillow_heif, "register_heif_opener", None)
        if callable(register_heif_opener):
            register_heif_opener()
        _pillow_openers_registered = True


def _imagecodecs_module() -> ModuleType | None:
    try:
        return importlib.import_module("imagecodecs")
    except ImportError:
        return None


def _decode_codec(module: ModuleType, decode_name: str, data: bytes) -> Any:
    decode = getattr(module, decode_name, None)
    if not callable(decode):
        raise OSError(f"缺少图像解码器：{decode_name}")
    return decode(data)


def _modern_codec_for_extension(extension: str) -> str | None:
    for codec_extension, decode_name in _MODERN_IMAGE_CODECS:
        if codec_extension == extension:
            return decode_name
    return None


def _read_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_header(path: Path) -> bytes | None:
    try:
        with path.open("rb") as file:
            return file.read(_HEADER_READ_SIZE)
    except OSError:
        return None


def _iso_bmff_brands(header: bytes) -> set[bytes]:
    if len(header) < 12 or header[4:8] != b"ftyp":
        return set()

    brands = {header[8:12]}
    for index in range(16, len(header) - 3, 4):
        brands.add(header[index : index + 4])
    return brands


def _modern_image_extension_from_header(header: bytes) -> str | None:
    if header.startswith(_JPEG_XL_CODESTREAM_MAGIC) or header.startswith(
        _JPEG_XL_CONTAINER_MAGIC
    ):
        return "jxl"

    if _iso_bmff_brands(header) & _AVIF_BRANDS:
        return "avif"

    return None


def _pillow_image_extension(path: Path) -> str | None:
    register_pillow_image_openers()
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=_PIL_AVIF_SUPPORT_WARNING,
                category=UserWarning,
            )
            with Image.open(path) as image:
                image_format = image.format
    except (OSError, SyntaxError):
        return None

    if image_format is None:
        return None
    return IMAGE_FORMAT_BY_PILLOW_FORMAT.get(image_format.upper(), image_format.lower())


def image_extension_from_file(path: Path) -> str | None:
    header = _read_header(path)
    if header is None:
        return None

    modern_extension = _modern_image_extension_from_header(header)
    if modern_extension is not None:
        return modern_extension

    return _pillow_image_extension(path)


def image_file_error(path: Path) -> str | None:
    if not path.is_file():
        return "not a file"

    header = _read_header(path)
    if header is None:
        return "cannot read file"

    modern_extension = _modern_image_extension_from_header(header)
    if modern_extension is not None:
        decode_name = _modern_codec_for_extension(modern_extension)
        if decode_name is None:
            return f"缺少图像解码器：{modern_extension}"
        imagecodecs = _imagecodecs_module()
        if imagecodecs is None:
            return "imagecodecs not installed"
        data = _read_file(path)
        if data is None:
            return "cannot read file"
        try:
            _decode_codec(imagecodecs, decode_name, data)
        except Exception as error:
            return str(error)
        return None

    register_pillow_image_openers()
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=_PIL_AVIF_SUPPORT_WARNING,
                category=UserWarning,
            )
            with Image.open(path) as image:
                image.verify()
    except (OSError, SyntaxError) as error:
        return str(error)

    return None


def image_file_is_valid(path: Path) -> bool:
    return image_file_error(path) is None


def open_image_for_processing(path: Path) -> Image.Image:
    header = _read_header(path)
    if header is None:
        raise OSError(f"cannot read file: {path}")

    modern_extension = _modern_image_extension_from_header(header)
    if modern_extension is not None:
        decode_name = _modern_codec_for_extension(modern_extension)
        if decode_name is None:
            raise OSError(f"缺少图像解码器：{modern_extension}")
        imagecodecs = _imagecodecs_module()
        if imagecodecs is None:
            raise OSError("imagecodecs not installed")
        data = _read_file(path)
        if data is None:
            raise OSError(f"cannot read file: {path}")
        decoded = _decode_codec(imagecodecs, decode_name, data)
        return Image.fromarray(decoded)

    register_pillow_image_openers()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=_PIL_AVIF_SUPPORT_WARNING,
            category=UserWarning,
        )
        with Image.open(path) as image:
            image.load()
            return image.copy()
