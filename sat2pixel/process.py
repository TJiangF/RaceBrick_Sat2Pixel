"""Semantic color mapping: edge extraction + fixed palette classification.

Pipeline (inspired by nongxl/motonav_stopwatch MapGeo / TdtRaster):
    1. grayscale gradient -> edge mask (black)
    2. HSV hue/saturation classification -> vegetation / water / ground / artificial
    3. remove small isolated fragments (noise from texture/icons)
    4. 3x3 majority-vote smoothing
    5. dilate edges so thin roads stay visible
"""

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage

# class ids (priority: edge > vegetation > water > ground > artificial)
EDGE = 0
VEGETATION = 1
WATER = 2
GROUND = 3
ARTIFICIAL = 4

CLASS_NAMES = ["edge", "vegetation", "water", "ground", "artificial"]

DEFAULT_COLORS = {
    "edge": (0, 0, 0),            # black
    "vegetation": (46, 204, 64),  # green
    "water": (0, 116, 217),       # blue
    "ground": (155, 155, 155),    # gray
    "artificial": (255, 193, 7),  # yellow
}

DEFAULT_PARAMS = {
    "blur": 0,              # gaussian blur radius before edge detection
    "edge_threshold": 15,   # 0..100 (percent of max gradient)
    "edge_thickness": 1,    # edge dilation radius (0..5)
    "veg_sat": 10,          # 0..100, min saturation for vegetation (green hue)
    "water_sat": 20,        # 0..100, min saturation for water (blue hue)
    "ground_sat": 18,       # 0..100, max saturation for ground/soil
    "min_fragment": 25,     # remove connected components smaller than this (px)
    "smooth": True,         # 3x3 majority-vote smoothing
    "detect_water": True,
}

_SENTINEL = 255
_K3 = np.ones((3, 3), dtype=np.int16)
_STRUCT4 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)


def _resolve_params(params):
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if k != "colors" and v is not None})
    return p


def _resolve_colors(params):
    colors = dict(DEFAULT_COLORS)
    if params and params.get("colors"):
        for name, hexval in params["colors"].items():
            if name in colors and hexval:
                hexval = hexval.lstrip("#")
                colors[name] = tuple(int(hexval[i : i + 2], 16) for i in (0, 2, 4))
    return colors


def _hsv(img):
    """RGB float32 (0..255) -> (h 0..360, s 0..1, v 0..1)."""
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
    return h, s, mx


def _edge_mask(img, params):
    lum = 0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]
    gray = Image.fromarray(lum.astype("uint8"))
    if params["blur"] > 0:
        gray = gray.filter(ImageFilter.GaussianBlur(radius=params["blur"]))
    arr = np.asarray(gray, dtype=np.float32)
    gy, gx = np.gradient(arr)
    mag = np.hypot(gx, gy)
    scale = 255.0 / (mag.max() + 1e-6)
    return (mag * scale) > (params["edge_threshold"] / 100.0) * 255.0


def _classify(img, params):
    """Classify non-edge pixels using HSV hue/saturation rules (ref MapGeo.classifyPixel)."""
    h, s, v = _hsv(img)
    lum = (0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]) / 255.0
    r = img[..., 0] / 255.0
    b = img[..., 2] / 255.0

    veg_sat = params["veg_sat"] / 100.0
    water_sat = params["water_sat"] / 100.0
    ground_sat = params["ground_sat"] / 100.0

    veg = (s > veg_sat) & (h >= 60) & (h <= 170)
    water = (s > water_sat) & (h >= 190) & (h <= 250) & (v < 0.55)
    ground = (s < ground_sat) & (r >= b) & (lum < 0.92)
    return veg, water, ground


def _remove_small(labels, min_fragment):
    """Merge connected components smaller than min_fragment into their neighbours."""
    if min_fragment <= 0:
        return labels
    out = labels.copy()
    remove = np.zeros(labels.shape, dtype=bool)
    for cls in range(5):
        mask = labels == cls
        if not mask.any():
            continue
        lab, n = ndimage.label(mask, structure=_STRUCT4)
        if n == 0:
            continue
        sizes = ndimage.sum(mask.astype(np.int32), lab, index=np.arange(1, n + 1))
        small = np.where(sizes < min_fragment)[0] + 1
        if small.size:
            remove |= np.isin(lab, small)
    if not remove.any():
        return out
    out[remove] = _SENTINEL
    return _fill_majority(out)


def _fill_majority(labels):
    """Replace sentinel pixels with the majority class of their neighbours."""
    out = labels.copy()
    unfilled = labels == _SENTINEL
    if not unfilled.any():
        return out
    while unfilled.any():
        cnt = np.zeros((labels.shape[0], labels.shape[1], 5), dtype=np.int16)
        for c in range(5):
            cnt[..., c] = ndimage.convolve(
                (out == c).astype(np.int16), _K3, mode="constant", cval=0
            )
        has_nb = cnt.sum(axis=-1) > 0
        upd = unfilled & has_nb
        if not upd.any():
            out[unfilled] = ARTIFICIAL
            break
        out[upd] = np.argmax(cnt[upd], axis=-1)
        unfilled &= ~upd
    return out


def _smooth(labels):
    """3x3 majority vote (>=5 of 9), preserving the current class otherwise."""
    cnt = np.zeros((labels.shape[0], labels.shape[1], 5), dtype=np.int16)
    for c in range(5):
        cnt[..., c] = ndimage.convolve(
            (labels == c).astype(np.int16), _K3, mode="constant", cval=0
        )
    best = np.argmax(cnt, axis=-1)
    best_cnt = cnt.max(axis=-1)
    out = labels.copy()
    out[best_cnt >= 5] = best[best_cnt >= 5]
    return out


def _dilate_class(labels, cls, thickness):
    """Dilate one class into its neighbours (ref MapGeo.dilateRoads)."""
    if thickness <= 0:
        return labels
    out = labels.copy()
    mask = labels == cls
    for _ in range(thickness):
        grow = ndimage.binary_dilation(mask, structure=_K3.astype(bool))
        out[grow & ~mask] = cls
        mask = grow
    return out


def _labels(image, params):
    p = _resolve_params(params)
    img = np.asarray(image.convert("RGB"), dtype=np.float32)

    edge = _edge_mask(img, p)
    veg, water, ground = _classify(img, p)

    labels = np.full(img.shape[:2], ARTIFICIAL, dtype=np.uint8)
    labels[ground] = GROUND
    if p["detect_water"]:
        labels[water] = WATER
    labels[veg] = VEGETATION
    labels[edge] = EDGE

    labels = _remove_small(labels, p["min_fragment"])
    if p["smooth"]:
        labels = _smooth(labels)
    labels = _dilate_class(labels, EDGE, p["edge_thickness"])
    return labels


def process(image, params=None):
    """Return an RGB PIL image with the fixed color mapping applied."""
    colors = _resolve_colors(params)
    labels = _labels(image, params)
    h, w = labels.shape
    out = np.zeros((h, w, 3), dtype=np.uint8)
    for cid, name in enumerate(CLASS_NAMES):
        out[labels == cid] = colors[name]
    return Image.fromarray(out, "RGB")


def classify_counts(image, params=None):
    """Return per-class pixel counts (for the UI legend)."""
    labels = _labels(image, params)
    total = labels.size
    return {name: int(np.sum(labels == cid)) for cid, name in enumerate(CLASS_NAMES)} | {
        "total": total
    }
