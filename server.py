"""RaceBrick_Sat2Pixel HTTP service.

- Production: POST /api/v1/satellite (ESP32 -> 4-color 2bpp image)
- Debug UI:   GET  /  (interactive map + semantic color mapping tuner)
- Geocode:    GET  /api/geocode?address=...
- Debug pipe: POST /api/debug (bbox + params -> semantic color PNG)
"""

import io
import json
import os
import time
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from sat2pixel.http import urlopen
from sat2pixel.mosaic import fetch_satellite
from sat2pixel.process import classify_counts, process
from sat2pixel.quantize import encode_bin, quantize_4color
from sat2pixel import protocol as P

AMAP_WEB_KEY = os.environ.get("AMAP_WEB_KEY", "")

app = FastAPI(title="RaceBrick_Sat2Pixel", version="0.2.0")

# (device_id, seq) -> (expires_at, body, media_type, headers)
_CACHE: dict = {}
# mosaic cache: (zoom, bbox) -> (expires_at, PIL image)
_MOSAIC: dict = {}
_MOSAIC_TTL = 300
_MOSAIC_MAX = 8


def _cache_get(key):
    item = _CACHE.get(key)
    if item is None:
        return None
    if item[0] < time.time():
        _CACHE.pop(key, None)
        return None
    return item


def _cache_put(key, body, media_type, headers):
    _CACHE[key] = (time.time() + P.DEDUP_TTL_S, body, media_type, headers)
    if len(_CACHE) > 512:
        for k in [k for k, v in _CACHE.items() if v[0] < time.time()]:
            _CACHE.pop(k, None)


def _mosaic_get_or_fetch(corners, zoom):
    key = (
        zoom,
        round(min(c["lng"] for c in corners), 5),
        round(min(c["lat"] for c in corners), 5),
        round(max(c["lng"] for c in corners), 5),
        round(max(c["lat"] for c in corners), 5),
    )
    item = _MOSAIC.get(key)
    if item and item[0] > time.time():
        return item[1], True
    image, zoom, meta = fetch_satellite(corners, zoom)
    _MOSAIC[key] = (time.time() + _MOSAIC_TTL, image)
    while len(_MOSAIC) > _MOSAIC_MAX:
        oldest = min(_MOSAIC, key=lambda k: _MOSAIC[k][0])
        _MOSAIC.pop(oldest, None)
    return image, False


def _palette_header(palette):
    return ",".join("#{:02X}{:02X}{:02X}".format(*rgb) for rgb in palette)


class Corner(BaseModel):
    lng: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)


class DebugRequest(BaseModel):
    corners: list[Corner] = Field(..., min_length=4, max_length=4)
    zoom: int | None = Field(None, ge=2, le=18)
    params: dict | None = None


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/api/geocode")
def geocode(address: str):
    if not AMAP_WEB_KEY:
        return JSONResponse(
            {"code": "NO_KEY", "message": "AMAP_WEB_KEY is not configured"},
            status_code=500,
        )
    url = "https://restapi.amap.com/v3/geocode/geo?" + urllib.parse.urlencode(
        {"address": address, "key": AMAP_WEB_KEY, "output": "JSON"}
    )
    try:
        with urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"code": "GEOCODE_FAILED", "message": str(exc)}, status_code=502)

    geos = data.get("geocodes") or []
    if not geos:
        return JSONResponse(
            {"code": "NOT_FOUND", "message": f"no result for '{address}'"},
            status_code=404,
        )
    lng, lat = geos[0]["location"].split(",")
    return {
        "lng": float(lng),
        "lat": float(lat),
        "formatted": geos[0].get("formatted_address", address),
    }


@app.post("/api/debug")
async def debug(req: DebugRequest):
    corners = [c.model_dump() for c in req.corners]
    try:
        image, _ = _mosaic_get_or_fetch(corners, req.zoom)
    except Exception as exc:  # noqa: BLE001
        return Response(
            content=f'{{"code":"TILE_FETCH_FAILED","message":"{exc}"}}',
            media_type="application/json",
            status_code=502,
        )

    params = req.params or {}
    result = process(image, params)
    counts = classify_counts(image, params)

    buf = io.BytesIO()
    result.save(buf, format="PNG", optimize=True)

    headers = {
        "X-Sat2Pixel-Width": str(result.width),
        "X-Sat2Pixel-Height": str(result.height),
        "X-Sat2Pixel-Classes": ",".join(f"{k}={v}" for k, v in counts.items()),
    }
    return Response(content=buf.getvalue(), media_type="image/png", headers=headers)


@app.post("/api/v1/satellite")
async def satellite(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=b'{"code":"INVALID_JSON","message":"body is not valid JSON"}',
            media_type="application/json",
            status_code=400,
        )

    try:
        req = P.SatelliteRequest(**body)
    except ValidationError as exc:
        return Response(content=exc.json(), media_type="application/json", status_code=422)

    key = (req.device_id, req.seq)
    cached = _cache_get(key)
    if cached is not None:
        _, cbody, ctype, cheaders = cached
        return Response(content=cbody, media_type=ctype, headers=cheaders)

    corners = [c.model_dump() for c in req.corners]
    try:
        image, zoom, meta = fetch_satellite(corners, req.zoom)
    except Exception as exc:  # noqa: BLE001
        return Response(
            content=f'{{"code":"TILE_FETCH_FAILED","message":"{exc}"}}',
            media_type="application/json",
            status_code=502,
        )

    if req.format == "png":
        indexed, palette = quantize_4color(image, req.palette)
        buf = io.BytesIO()
        indexed.save(buf, format="PNG", optimize=True)
        body_bytes = buf.getvalue()
        media_type = "image/png"
    else:
        body_bytes, palette = encode_bin(image, req.palette)
        media_type = "application/octet-stream"

    headers = {
        "X-Sat2Pixel-Seq": str(req.seq),
        "X-Sat2Pixel-Width": str(image.width),
        "X-Sat2Pixel-Height": str(image.height),
        "X-Sat2Pixel-Zoom": str(zoom),
        "X-Sat2Pixel-Tiles": str(meta["tile_count"]),
        "X-Sat2Pixel-Palette": _palette_header(palette),
    }
    _cache_put(key, body_bytes, media_type, headers)
    return Response(content=body_bytes, media_type=media_type, headers=headers)


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
