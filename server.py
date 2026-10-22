"""RaceBrick_Sat2Pixel HTTP service.

- Production: POST /api/v1/satellite (ESP32 -> <=240x240 compressed image)
- Preview:    POST /api/preview (bbox + options -> downsampled image)
- Geocode:    GET  /api/geocode?address=...
- Debug UI:   GET  /  (interactive map + 240x240 preview)
"""

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
from sat2pixel.preview import downsample, encode, stats
from sat2pixel.protocol import Corner, SatelliteRequest, DEDUP_TTL_S

AMAP_WEB_KEY = os.environ.get("AMAP_WEB_KEY", "")

app = FastAPI(title="RaceBrick_Sat2Pixel", version="0.4.0")

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
    _CACHE[key] = (time.time() + DEDUP_TTL_S, body, media_type, headers)
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


class PreviewRequest(BaseModel):
    corners: list[Corner] = Field(..., min_length=4, max_length=4)
    zoom: int | None = Field(None, ge=2, le=18)
    max_size: int = Field(240, ge=16, le=1024)
    method: str = Field("lanczos", pattern="^(lanczos|box|bilinear|nearest)$")
    sharpen: float = Field(0.4, ge=0.0, le=3.0)
    format: str = Field("png", pattern="^(png|jpeg|rgb565)$")
    quality: int = Field(85, ge=1, le=100)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": "0.4.0"}


@app.get("/api/inputtips")
def inputtips(keywords: str):
    if not AMAP_WEB_KEY:
        return JSONResponse(
            {"code": "NO_KEY", "message": "AMAP_WEB_KEY is not configured"},
            status_code=500,
        )
    url = "https://restapi.amap.com/v3/assistant/inputtips?" + urllib.parse.urlencode(
        {"keywords": keywords, "key": AMAP_WEB_KEY, "citylimit": "false", "output": "JSON"}
    )
    try:
        with urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"code": "INPUTTIPS_FAILED", "message": str(exc)}, status_code=502
        )

    tips = []
    for t in data.get("tips") or []:
        loc = t.get("location")
        if not isinstance(loc, str) or not loc:
            continue
        lng, _, lat = loc.partition(",")
        tips.append(
            {
                "name": t.get("name"),
                "district": t.get("district"),
                "address": t.get("address"),
                "lng": float(lng),
                "lat": float(lat),
            }
        )
    return {"tips": tips}


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


@app.post("/api/preview")
async def preview(req: PreviewRequest):
    corners = [c.model_dump() for c in req.corners]
    try:
        image, _ = _mosaic_get_or_fetch(corners, req.zoom)
    except Exception as exc:  # noqa: BLE001
        return Response(
            content=f'{{"code":"TILE_FETCH_FAILED","message":"{exc}"}}',
            media_type="application/json",
            status_code=502,
        )

    small = downsample(image, req.max_size, req.method, req.sharpen)
    st = stats(small, req.quality)
    body, media_type = encode(small, req.format, req.quality)

    headers = {
        "X-Preview-Width": str(st["width"]),
        "X-Preview-Height": str(st["height"]),
        "X-Preview-Png": str(st["png"]),
        "X-Preview-Jpeg": str(st["jpeg"]),
        "X-Preview-Rgb565": str(st["rgb565"]),
    }
    return Response(content=body, media_type=media_type, headers=headers)


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
        req = SatelliteRequest(**body)
    except ValidationError as exc:
        return Response(content=exc.json(), media_type="application/json", status_code=422)

    key = (req.device_id, req.seq)
    cached = _cache_get(key)
    if cached is not None:
        _, cbody, ctype, cheaders = cached
        return Response(content=cbody, media_type=ctype, headers=cheaders)

    corners = [c.model_dump() for c in req.corners]
    try:
        image, _ = _mosaic_get_or_fetch(corners, None)
    except Exception as exc:  # noqa: BLE001
        return Response(
            content=f'{{"code":"TILE_FETCH_FAILED","message":"{exc}"}}',
            media_type="application/json",
            status_code=502,
        )

    small = downsample(image, req.max_size)
    body_bytes, media_type = encode(small, req.format)

    headers = {
        "X-Sat2Pixel-Seq": str(req.seq),
        "X-Sat2Pixel-Width": str(small.width),
        "X-Sat2Pixel-Height": str(small.height),
        "X-Sat2Pixel-Size": str(len(body_bytes)),
    }
    _cache_put(key, body_bytes, media_type, headers)
    return Response(content=body_bytes, media_type=media_type, headers=headers)


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
