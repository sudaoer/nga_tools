from __future__ import annotations

import importlib
import threading
import warnings
from pathlib import Path
from types import ModuleType

from PIL import Image

_PIL_OPENERS_LOCK = threading.Lock()
_pillow_openers_registered = False
_PIL_AVIF_SUPPORT_WARNING = (
    "image file could not be identified because AVIF support not installed"
)
_MODERN_IMAGE_CODECS = (
    ("avif", "avif_check", "avif_decode"),
    ("jxl", "jpegxl_check", "jpegxl_decode"),
)

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


def _codec_check(module: ModuleType, check_name: str, data: bytes) -> bool:
    check = getattr(module, check_name, None)
    if not callable(check):
        return False
    try:
        return bool(check(data))
    except Exception:
        return False


def _decode_codec(module: ModuleType, decode_name: str, data: bytes) -> None:
    decode = getattr(module, decode_name, None)
    if not callable(decode):
        raise OSError(f"缺少图像解码器：{decode_name}")
    decode(data)


def _modern_image_codec(data: bytes) -> tuple[str, str] | None:
    imagecodecs = _imagecodecs_module()
    if imagecodecs is None:
        return None

    for extension, check_name, decode_name in _MODERN_IMAGE_CODECS:
        if _codec_check(imagecodecs, check_name, data):
            return extension, decode_name
    return None


def _read_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
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
    except OSError:
        return None

    if image_format is None:
        return None
    return IMAGE_FORMAT_BY_PILLOW_FORMAT.get(image_format.upper(), image_format.lower())


def image_extension_from_file(path: Path) -> str | None:
    data = _read_file(path)
    if data is None:
        return None

    modern_codec = _modern_image_codec(data)
    if modern_codec is not None:
        extension, _decode_name = modern_codec
        return extension

    return _pillow_image_extension(path)


def image_file_error(path: Path) -> str | None:
    if not path.is_file():
        return "not a file"

    data = _read_file(path)
    if data is None:
        return "cannot read file"

    modern_codec = _modern_image_codec(data)
    if modern_codec is not None:
        _extension, decode_name = modern_codec
        imagecodecs = _imagecodecs_module()
        if imagecodecs is None:
            return "imagecodecs not installed"
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
