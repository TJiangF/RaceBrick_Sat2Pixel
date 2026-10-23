"""MotoNav-style navigation-image compression.

Reference: zyn950929/MotoNav
  * OriginalMapColors.java — weighted median-cut of the actual pixels into
    <=16 colours: quantise to 5-5-5, build a 32768-bin histogram, repeatedly
    split the box with the largest (range * sqrt(pixelCount)) at its weighted
    median, palette = weighted mean colour per box (packed as RGB565).
  * MapGeo.encodeRaster — per-pixel colour index run-length encoding.

Wire packet (little-endian):
  0    2   width         (uint16 LE)
  2    2   height        (uint16 LE)
  4    2   palette count (uint16 LE) = 16
  6    32  palette: 16 x RGB565 (uint16 LE)
  38   N   RLE: byte = (run-1)<<4 | index ; if (run-1)==15 -> next byte = run-16
"""

import math
import struct

import numpy as np


def _comp(keys, axis):
    return (keys >> (10 - axis * 5)) & 31


def _make_box(keys, hist):
    total = int(hist[keys].sum())
    axis, rng = 0, 0
    for a in range(3):
        vals = _comp(keys, a)
        r = int(vals.max() - vals.min())
        if r > rng:
            rng, axis = r, a
    score = rng * math.sqrt(total) if len(keys) > 1 else -1.0
    return {"keys": keys, "axis": axis, "range": rng, "total": total, "score": score}


def quantize16(image):
    """Return (indices uint8 HxW, palette uint16[16] RGB565) via weighted median-cut."""
    a = np.asarray(image.convert("RGB"), dtype=np.uint16)
    r = (a[..., 0] >> 3) & 31
    g = (a[..., 1] >> 3) & 31
    b = (a[..., 2] >> 3) & 31
    keys = (r << 10) | (g << 5) | b
    flat = keys.ravel()
    hist = np.bincount(flat, minlength=32768).astype(np.int64)
    used = np.nonzero(hist)[0]

    boxes = [_make_box(used, hist)]
    while len(boxes) < 16:
        best = max(range(len(boxes)), key=lambda i: boxes[i]["score"])
        box = boxes[best]
        if len(box["keys"]) < 2:
            break
        axis = box["axis"]
        order = np.argsort(_comp(box["keys"], axis), kind="stable")
        ks = box["keys"][order]
        cum = np.cumsum(hist[ks])
        cut = int(np.searchsorted(cum, box["total"] / 2.0)) + 1
        cut = max(1, min(len(ks) - 1, cut))
        boxes.pop(best)
        boxes.append(_make_box(ks[:cut], hist))
        boxes.append(_make_box(ks[cut:], hist))

    palette = np.zeros(16, dtype=np.uint16)
    lookup = np.zeros(32768, dtype=np.uint8)
    for i, box in enumerate(boxes):
        ks = box["keys"]
        w = hist[ks]
        tot = w.sum()
        rr = int(round(float((_comp(ks, 0) * w).sum()) / tot)) & 31
        gg = int(round(float((_comp(ks, 1) * w).sum()) / tot)) & 31
        bb = int(round(float((_comp(ks, 2) * w).sum()) / tot)) & 31
        palette[i] = (rr << 11) | (((gg << 1) | (gg >> 4)) << 5) | bb
        lookup[ks] = i
    for i in range(len(boxes), 16):
        palette[i] = palette[0]

    indices = lookup[flat].reshape(keys.shape)
    return indices, palette


def rle_encode(indices):
    flat = indices.ravel()
    n = flat.size
    out = bytearray()
    i = 0
    while i < n:
        c = int(flat[i]) & 15
        run = 1
        while i + run < n and run < 271 and (int(flat[i + run]) & 15) == c:
            run += 1
        if run <= 15:
            out.append(((run - 1) << 4) | c)
        else:
            out.append(0xF0 | c)
            out.append(run - 16)
        i += run
    return bytes(out)


def encode_nav_packet(image):
    """Compress a navigation image. Returns (packet bytes, palette, width, height)."""
    indices, palette = quantize16(image)
    h, w = indices.shape
    out = bytearray()
    out += struct.pack("<HHH", w, h, 16)
    for p in palette:
        out += struct.pack("<H", int(p))
    out += rle_encode(indices)
    return bytes(out), palette, w, h


def rgb_to_565(c):
    r, g, b = c
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def encode_indexed_packet(indices, palette_rgb):
    """Encode an already-classified index map with a given RGB palette (pixel style)."""
    h, w = indices.shape
    out = bytearray()
    out += struct.pack("<HHH", w, h, len(palette_rgb))
    for c in palette_rgb:
        out += struct.pack("<H", rgb_to_565(c))
    out += rle_encode(indices)
    return bytes(out)


def palette565_to_rgb(p):
    r = (int(p) >> 11) & 31
    g = (int(p) >> 5) & 63
    b = int(p) & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def decode_nav_packet(packet):
    """Decode a packet to an RGB PIL image (preview/debug)."""
    from PIL import Image

    w, h, palcnt = struct.unpack_from("<HHH", packet, 0)
    palette = list(struct.unpack_from("<%dH" % palcnt, packet, 6))
    off = 6 + palcnt * 2
    total = w * h
    idx = bytearray(total)
    p = 0
    while off < len(packet) and p < total:
        v = packet[off]
        off += 1
        run = (v >> 4) + 1
        if (v >> 4) == 15:
            run = 16 + packet[off]
            off += 1
        c = v & 15
        for _ in range(run):
            if p < total:
                idx[p] = c
                p += 1
    rgbpal = [palette565_to_rgb(c) for c in palette]
    img = Image.new("RGB", (w, h))
    img.putdata([rgbpal[i] for i in idx])
    return img
