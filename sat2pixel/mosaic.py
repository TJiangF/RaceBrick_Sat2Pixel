"""Fetch map tiles (AMap satellite / Tianditu road+satellite) and stitch them."""

import io
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from sat2pixel.http import urlopen

TILE_SIZE = 256
MAX_ZOOM = 18
MIN_ZOOM = 2

TIANDITU_KEY = os.environ.get("TIANDITU_KEY", "")

# layer -> source config. datum: AMap = GCJ-02, Tianditu = WGS-84.
LAYERS = {
    "satellite": {  # AMap satellite
        "type": "amap",
        "hosts": [f"webst0{i}.is.autonavi.com" for i in range(1, 5)],
        "query": "style=6",
        "datum": "gcj02",
    },
    "road": {  # Tianditu vector base map (no labels) — like MotoNav
        "type": "tdt",
        "tdt_layer": "vec_w",
        "datum": "wgs84",
    },
    "amap_road": {  # AMap road map (with labels)
        "type": "amap",
        "hosts": [f"webrd0{i}.is.autonavi.com" for i in range(1, 5)],
        "query": "lang=zh_cn&size=1&scale=1&style=8",
        "datum": "gcj02",
    },
    "tdt_satellite": {  # Tianditu satellite imagery
        "type": "tdt",
        "tdt_layer": "img_w",
        "datum": "wgs84",
    },
}
DEFAULT_LAYER = "satellite"


def layer_datum(layer):
    return LAYERS.get(layer, LAYERS[DEFAULT_LAYER]).get("datum", "gcj02")


def tile_url(x, y, zoom, layer):
    cfg = LAYERS.get(layer, LAYERS[DEFAULT_LAYER])
    if cfg["type"] == "tdt":
        if not TIANDITU_KEY:
            raise RuntimeError("TIANDITU_KEY is not configured")
        host = f"t{(x + y) % 8}.tianditu.gov.cn"
        return f"https://{host}/DataServer?T={cfg['tdt_layer']}&x={x}&y={y}&l={zoom}&tk={TIANDITU_KEY}"
    host = cfg["hosts"][(x + y) % 4]
    return f"https://{host}/appmaptile?{cfg['query']}&x={x}&y={y}&z={zoom}"


def tile_x(lng: float, zoom: int) -> float:
    return (lng + 180.0) / 360.0 * (1 << zoom)


def tile_y(lat: float, zoom: int) -> float:
    r = math.radians(lat)
    return (
        1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi
    ) / 2.0 * (1 << zoom)


def _grid(pts, zoom):
    lngs = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (
        math.floor(tile_x(min(lngs), zoom)),
        math.floor(tile_y(max(lats), zoom)),
        math.floor(tile_x(max(lngs), zoom)),
        math.floor(tile_y(min(lats), zoom)),
    )


def _fits(grid, max_tiles, max_dim):
    x0, y0, x1, y1 = grid
    w, h = x1 - x0 + 1, y1 - y0 + 1
    return w <= max_dim and h <= max_dim and w * h <= max_tiles


def _candidate_zooms(pts, max_tiles=48, max_dim=8):
    zooms = []
    for zoom in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        if _fits(_grid(pts, zoom), max_tiles, max_dim):
            zooms.append(zoom)
    return zooms


def _fetch_one(tile, layer):
    x, y, zoom = tile
    url = tile_url(x, y, zoom, layer)
    last = None
    for _ in range(3):
        try:
            with urlopen(url, timeout=15) as resp:
                return x, y, resp.read()
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            rd = getattr(exc, "read", None)
            if rd:
                try:
                    detail = rd()[:200].decode("utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    pass
            last = detail
    raise RuntimeError(f"tile fetch failed {url}: {last}")


def _is_blank(image, threshold=6.0):
    """Detect 'no data' placeholder tiles (near-uniform gray)."""
    gray = image.convert("L").resize((32, 32))
    px = list(gray.getdata())
    mean = sum(px) / len(px)
    variance = sum((p - mean) ** 2 for p in px) / len(px)
    return variance < threshold**2


def _stitch(pts, zoom, layer=DEFAULT_LAYER):
    x0, y0, x1, y1 = _grid(pts, zoom)
    coords = [(x, y, zoom) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    tiles = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one, c, layer): c for c in coords}
        for fut in as_completed(futures):
            x, y, data = fut.result()
            tiles[(x, y)] = Image.open(io.BytesIO(data)).convert("RGB")

    width = (x1 - x0 + 1) * TILE_SIZE
    height = (y1 - y0 + 1) * TILE_SIZE
    canvas = Image.new("RGB", (width, height))
    for (x, y), im in tiles.items():
        canvas.paste(im, ((x - x0) * TILE_SIZE, (y - y0) * TILE_SIZE))

    mn_lng = min(p[0] for p in pts)
    mx_lng = max(p[0] for p in pts)
    mn_lat = min(p[1] for p in pts)
    mx_lat = max(p[1] for p in pts)
    px0 = (tile_x(mn_lng, zoom) - x0) * TILE_SIZE
    px1 = (tile_x(mx_lng, zoom) - x0) * TILE_SIZE
    py0 = (tile_y(mx_lat, zoom) - y0) * TILE_SIZE
    py1 = (tile_y(mn_lat, zoom) - y0) * TILE_SIZE
    cx0, cy0 = int(px0), int(py0)
    cx1, cy1 = int(px1), int(py1)
    cx1 = max(cx1, cx0 + 1)   # guarantee a non-empty crop
    cy1 = max(cy1, cy0 + 1)
    canvas = canvas.crop((cx0, cy0, cx1, cy1))

    meta = {
        "zoom": zoom,
        "tile_count": len(coords),
        "grid": (x1 - x0 + 1, y1 - y0 + 1),
    }
    return canvas, meta


def fetch_satellite(corners, zoom=None, layer=DEFAULT_LAYER):
    """Return (image, zoom, meta) covering the given corner points."""
    pts = [(c["lng"], c["lat"]) for c in corners]
    if zoom is not None:
        image, meta = _stitch(pts, zoom, layer)
        return image, zoom, meta

    # auto: try the highest workable zoom, fall back if tiles are blank
    last = None
    for z in _candidate_zooms(pts):
        image, meta = _stitch(pts, z, layer)
        if layer != "satellite" or z == MIN_ZOOM or not _is_blank(image):
            return image, z, meta
        last = (image, z, meta)
    return last
