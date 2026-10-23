"""MotoNav "pixel" map style: 4-class semantic + flat palette.

Reference: zyn950929/MotoNav
  * MapGeo.classifyPixel / backgroundLum  -> block / road / park / water
  * MapGeo.cleanRaster                   -> drop tiny specks + 3x3 majority vote
  * MapGeo.dilateRoads                   -> thicken roads into blocks
  * AmapSdkRaster palette                 -> flat colours:
        block #DCDCDC, road #303030, park #5BBF5B, water #4A90D9

This is what gives the "pixel art" look (conventional weighted median-cut keeps
the original AMap appearance instead).
"""

import numpy as np
from PIL import Image
from scipy import ndimage

PX_BLOCK, PX_ROAD, PX_PARK, PX_WATER = 0, 1, 2, 3

PALETTE_RGB = [
    (0xDC, 0xDC, 0xDC),  # block  (light grey background)
    (0x30, 0x30, 0x30),  # road   (near black)
    (0x5B, 0xBF, 0x5B),  # park   (green)
    (0x4A, 0x90, 0xD9),  # water  (blue)
]

_STRUCT4 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
_K3 = np.ones((3, 3), dtype=np.int16)
_K3B = np.ones((3, 3), dtype=bool)


def _hsv(img):
    r = img[..., 0] / 255.0
    g = img[..., 1] / 255.0
    b = img[..., 2] / 255.0
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    with np.errstate(divide="ignore", invalid="ignore"):
        h = np.where(
            mx == mn, 0.0,
            np.where(
                mx == r, 60.0 * np.mod((g - b) / d, 6.0),
                np.where(mx == g, 60.0 * ((b - r) / d + 2.0), 60.0 * ((r - g) / d + 4.0)),
            ),
        )
        s = np.where(mx == 0, 0.0, d / np.maximum(mx, 1e-9))
    h = np.where(h < 0, h + 360.0, h)
    return h, s


def _lum(img):
    return (img[..., 0] * 30 + img[..., 1] * 59 + img[..., 2] * 11) // 100


def background_lum(img):
    """Most common luminance among low-saturation pixels (the block background)."""
    mx = img.max(axis=-1)
    mn = img.min(axis=-1)
    low_sat = (mx > 0) & ((mx - mn) * 100 // np.maximum(mx, 1) < 8)
    if not low_sat.any():
        return 240
    hist = np.bincount(_lum(img)[low_sat].ravel(), minlength=256)
    return int(hist.argmax())


def classify(img):
    h, s = _hsv(img)
    lum = _lum(img)
    bg = background_lum(img)
    water = (s > 0.12) & (h >= 180) & (h <= 255)
    park = (s > 0.10) & (h >= 60) & (h <= 170)
    road = ((s > 0.18) & (h >= 10) & (h <= 60)) | (lum >= bg + 6)
    labels = np.full(img.shape[:2], PX_BLOCK, dtype=np.uint8)
    labels[road] = PX_ROAD
    labels[park] = PX_PARK
    labels[water] = PX_WATER
    return labels


def clean(labels, min_road=60, min_area=40):
    out = labels.copy()
    for cls, limit in ((PX_ROAD, min_road), (PX_PARK, min_area), (PX_WATER, min_area)):
        mask = out == cls
        if not mask.any():
            continue
        lab, n = ndimage.label(mask, structure=_STRUCT4)
        if n == 0:
            continue
        sizes = np.bincount(lab.ravel())
        small = np.where(sizes < limit)[0]
        small = small[small != 0]
        if small.size:
            out[np.isin(lab, small)] = PX_BLOCK
    cnt = np.zeros((out.shape[0], out.shape[1], 4), dtype=np.int16)
    for c in range(4):
        cnt[..., c] = ndimage.convolve((out == c).astype(np.int16), _K3, mode="constant", cval=0)
    best = np.argmax(cnt, axis=-1)
    best_cnt = cnt.max(axis=-1)
    upd = best_cnt >= 5
    out[upd] = best[upd]
    return out


def dilate_roads(labels):
    road = labels == PX_ROAD
    grow = ndimage.binary_dilation(road, structure=_K3B)
    out = labels.copy()
    out[grow & (labels == PX_BLOCK)] = PX_ROAD
    return out


def render(image):
    """Return (RGB PIL image, labels uint8 HxW) in the pixel style."""
    img = np.asarray(image.convert("RGB"), dtype=np.uint8)
    labels = dilate_roads(clean(classify(img)))
    out = np.zeros((labels.shape[0], labels.shape[1], 3), dtype=np.uint8)
    for i, c in enumerate(PALETTE_RGB):
        out[labels == i] = c
    return Image.fromarray(out, "RGB"), labels
