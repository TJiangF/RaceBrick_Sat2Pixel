"""High-quality downsampling of satellite imagery to a small target size.

For a small device screen (<=240x240) the goal is to preserve as much detail
as possible while keeping the payload tiny enough to send over WiFi:
    - Lanczos resampling (anti-aliased, sharpest for downsampling)
    - optional mild unsharp mask to restore edge contrast
    - encodings: PNG (lossless preview), JPEG (smallest), RGB565 (raw 16-bit
      framebuffer for the MCU, no codec needed)
"""

import io

import numpy as np
from PIL import Image, ImageFilter

METHODS = {
    "lanczos": Image.Resampling.LANCZOS,
    "box": Image.Resampling.BOX,
    "bilinear": Image.Resampling.BILINEAR,
    "nearest": Image.Resampling.NEAREST,
}


def fit_size(w, h, max_size):
    """Fit inside max_size x max_size, preserving aspect ratio."""
    scale = min(1.0, max_size / max(w, h))
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def downsample(image, max_size=240, method="lanczos", sharpen=0.0):
    w, h = fit_size(image.width, image.height, max_size)
    out = image.convert("RGB").resize((w, h), METHODS.get(method, Image.Resampling.LANCZOS))
    if sharpen > 0:
        out = out.filter(ImageFilter.UnsharpMask(radius=2, percent=int(sharpen * 100), threshold=2))
    return out


def to_rgb565(image, byteorder="big"):
    """Pack an RGB image into 16-bit RGB565 bytes (R5 G6 B5)."""
    a = np.asarray(image.convert("RGB"), dtype=np.uint16)
    r = ((a[..., 0] >> 3) & 0x1F) << 11
    g = ((a[..., 1] >> 2) & 0x3F) << 5
    b = (a[..., 2] >> 3) & 0x1F
    v = r | g | b
    return v.astype(">u2" if byteorder == "big" else "<u2").tobytes()


def encode(image, fmt="png", quality=85):
    """Return (bytes, media_type) for the requested format."""
    if fmt == "rgb565":
        return to_rgb565(image), "application/octet-stream"
    buf = io.BytesIO()
    if fmt == "jpeg":
        image.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg"
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), "image/png"


def stats(image, quality=85):
    """Byte sizes of the three encodings (for the UI comparison)."""
    return {
        "width": image.width,
        "height": image.height,
        "png": len(encode(image, "png")[0]),
        "jpeg": len(encode(image, "jpeg", quality)[0]),
        "rgb565": image.width * image.height * 2,
    }
