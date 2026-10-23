"""MotoNav "pixel" map style: 4-class semantic + themeable flat palette.

Reference: zyn950929/MotoNav
  * MapGeo.classifyPixel / backgroundLum -> block / road / park / water
  * MapGeo.cleanRaster                   -> drop tiny specks + 3x3 majority vote
  * MapGeo.dilateRoads                   -> thicken roads into blocks
  * firmware MapTheme table               -> 4-colour theme palettes (RGB565)

The theme palette is what gives the different looks: theme 7 掌机像素 is the
Game Boy green look, theme 1 罪恶都市 the Vice City look, etc. The neutral
palette is theme 0 原版.
"""

import numpy as np
from PIL import Image
from scipy import ndimage

PX_BLOCK, PX_ROAD, PX_PARK, PX_WATER = 0, 1, 2, 3

_STRUCT4 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
_K3 = np.ones((3, 3), dtype=np.int16)
_K3B = np.ones((3, 3), dtype=bool)


def _rgb565(c):
    r = (c >> 11) & 31
    g = (c >> 5) & 63
    b = c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


# block, road, park, water (RGB565), copied from firmware MapTheme table
THEMES_565 = {
    "original": [0xDEFB, 0x3186, 0x5DEB, 0x4C9B],      # 0 原版
    "vice": [0xE6D9, 0x3187, 0x760D, 0x4E3B],          # 1 罪恶都市
    "gta": [0x3A08, 0x9D15, 0x4327, 0x2A8F],           # 2 GTA5
    "cyber": [0x0021, 0x04B6, 0x0103, 0x1847],         # 3 赛博霓虹
    "paper": [0xEF18, 0xF5CD, 0xC6B3, 0x9E3A],         # 4 复古纸地图
    "minimal": [0x0000, 0x8410, 0x0000, 0x0000],       # 5 极简黑白
    "blueprint": [0x094B, 0xDF5F, 0x1A50, 0x2B35],     # 6 蓝图
    "gameboy": [0x8E0E, 0xE7DA, 0x334A, 0x08C4],       # 7 掌机像素
    "radar": [0x00C1, 0x0B46, 0x0183, 0x0084],         # 8 雷达
    "compass": [0x0863, 0x29AA, 0x08C4, 0x08A6],       # 9 寻宝罗盘
    "satellite_alt": [0x5B08, 0x8C50, 0x3285, 0x29E8], # 10 卫星替代
}

THEMES_RGB = {name: [_rgb565(c) for c in pal] for name, pal in THEMES_565.items()}

THEME_LABELS = {
    "gameboy": "掌机像素 (GameBoy)",
    "original": "原版",
    "vice": "罪恶都市",
    "gta": "GTA5",
    "cyber": "赛博霓虹",
    "paper": "复古纸地图",
    "minimal": "极简黑白",
    "blueprint": "蓝图",
    "radar": "雷达",
    "compass": "寻宝罗盘",
    "satellite_alt": "卫星替代",
}

DEFAULT_THEME = "gameboy"


def palette(theme):
    return THEMES_RGB.get(theme, THEMES_RGB[DEFAULT_THEME])


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


def render(image, theme=DEFAULT_THEME):
    """Return (RGB PIL image, labels uint8 HxW) in the pixel style for a theme."""
    img = np.asarray(image.convert("RGB"), dtype=np.uint8)
    labels = dilate_roads(clean(classify(img)))
    colors = palette(theme)
    out = np.zeros((labels.shape[0], labels.shape[1], 3), dtype=np.uint8)
    for i, c in enumerate(colors):
        out[labels == i] = c
    return Image.fromarray(out, "RGB"), labels
