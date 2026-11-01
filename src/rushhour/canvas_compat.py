"""Compatibility for canvas integrations using Streamlit's removed image helper."""

from __future__ import annotations

import base64
import importlib
import io
import sys
from types import ModuleType
from typing import Any


def install_image_to_url_compat(image_module: ModuleType | None = None) -> None:
    """Install an inline image encoder only when the legacy helper is unavailable."""
    if image_module is None:
        try:
            image_module = importlib.import_module("streamlit.elements.image")
        except ModuleNotFoundError as error:
            if error.name != "streamlit.elements.image":
                raise
            image_module = ModuleType("streamlit.elements.image")
            sys.modules[image_module.__name__] = image_module
            elements = importlib.import_module("streamlit.elements")
            elements.__dict__["image"] = image_module
    if not hasattr(image_module, "image_to_url"):
        image_module.__dict__["image_to_url"] = _image_to_data_url


def _image_to_data_url(
    image: Any,
    width: int | None = None,
    clamp: bool = False,
    channels: str = "RGB",
    output_format: str = "PNG",
    image_id: str | None = None,
) -> str:
    del width, clamp, image_id
    if isinstance(image, (bytes, bytearray)):
        raw = bytes(image)
        mime = _mime_type(raw)
    else:
        from PIL import Image

        if not isinstance(image, Image.Image):
            image = Image.fromarray(image)
        converted = image.convert("RGBA" if channels.upper() == "RGBA" else "RGB")
        buffer = io.BytesIO()
        converted.save(buffer, format=output_format)
        raw = buffer.getvalue()
        mime = Image.MIME.get(output_format.upper(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _mime_type(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.lstrip().startswith(b"<svg"):
        return "image/svg+xml"
    return "application/octet-stream"
