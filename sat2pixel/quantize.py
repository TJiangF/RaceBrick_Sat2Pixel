"""4-color (2-bit) compression for satellite imagery."""

import struct

GRAY4_PALETTE = [(255, 255, 255), (170, 170, 170), (85, 85, 85), (0, 0, 0)]


def quantize_4color(image, mode="adaptive"):
    """Quantize an RGB image to a 4-color palette.

    mode="adaptive": median-cut on the image's own dominant colors.
    mode="gray4":    fixed 4-level grayscale.
    Returns (indexed_image, palette) where palette is a list of 4 RGB tuples.
    """
    if mode == "gray4":
        gray = image.convert("L")
        palette = GRAY4_PALETTE
        levels = [0, 85, 170, 255]
        lut = [0] * 256
        for value in range(256):
            best = min(levels, key=lambda l: abs(l - value))
            lut[value] = levels.index(best)
        indexed = gray.point(lut, mode="P")
        indexed.putpalette([c for rgb in palette for c in rgb])
        return indexed, palette

    indexed = image.quantize(colors=4, method=2)  # MEDIANCUT
    raw = indexed.getpalette()[: 4 * 3]
    palette = [tuple(raw[i : i + 3]) for i in range(0, len(raw), 3)]
    return indexed, palette


def pack_2bpp(indexed, width, height):
    """Pack an indexed image (values 0..3) into 2 bits per pixel, row-major,
    each row padded to a byte boundary."""
    data = list(indexed.getdata())
    row_bytes = (width + 3) // 4
    out = bytearray()
    for r in range(height):
        rowbuf = bytearray(row_bytes)
        for i in range(width):
            value = data[r * width + i] & 0x03
            rowbuf[i // 4] |= value << (6 - (i % 4) * 2)
        out += rowbuf
    return bytes(out)


def to_bin(width, height, palette, packed_pixels):
    """Serialise to the RaceBrick_Sat2Pixel .bin wire format."""
    header = struct.pack(">HH", width, height)
    palette_bytes = b"".join(bytes(rgb) for rgb in palette)
    return header + palette_bytes + packed_pixels


def encode_bin(image, mode="adaptive"):
    """Full pipeline: RGB image -> 4-color 2bpp .bin bytes + metadata."""
    indexed, palette = quantize_4color(image, mode)
    w, h = indexed.size
    packed = pack_2bpp(indexed, w, h)
    return to_bin(w, h, palette, packed), palette
