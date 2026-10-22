"""Semantic color mapping: edge extraction + fixed palette classification.

Pipeline: satellite RGB -> grayscale gradient edges (black) -> classify the
rest into vegetation / water / ground / artificial with fixed colors.
"""

import numpy as np
from PIL import Image, ImageFilter

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
    "edge_thickness": 1,    # extra dilation pixels (0..5)
    "veg_threshold": 15,    # greenness 2G-R-B above this -> vegetation
    "water_threshold": 15,  # B-R and B-G above this -> water
    "ground_sat": 60,       # max saturation to count as ground (soil)
    "detect_water": True,
}


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


def _labels(image, params):
    p = _resolve_params(params)
    img = np.asarray(image.convert("RGB"), dtype=np.float32)
    h, w, _ = img.shape
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b

    gray = Image.fromarray(lum.astype("uint8"))
    if p["blur"] > 0:
        gray = gray.filter(ImageFilter.GaussianBlur(radius=p["blur"]))
    larr = np.asarray(gray, dtype=np.float32)

    gy, gx = np.gradient(larr)
    mag = np.hypot(gx, gy)
    scale = 255.0 / (mag.max() + 1e-6)
    edge = (mag * scale) > (p["edge_threshold"] / 100.0) * 255.0
    if p["edge_thickness"] > 0:
        size = 2 * p["edge_thickness"] + 1
        em = Image.fromarray((edge * 255).astype("uint8")).filter(ImageFilter.MaxFilter(size))
        edge = np.asarray(em) > 0

    greenness = 2.0 * g - r - b
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    sat = maxc - minc

    veg = greenness > p["veg_threshold"]
    water = (b - r > p["water_threshold"]) & (b - g > p["water_threshold"])
    ground = (sat < p["ground_sat"]) & (r >= b) & (lum < 225)

    labels = np.full((h, w), ARTIFICIAL, dtype=np.uint8)
    labels[ground] = GROUND
    if p["detect_water"]:
        labels[water] = WATER
    labels[veg] = VEGETATION
    labels[edge] = EDGE
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
