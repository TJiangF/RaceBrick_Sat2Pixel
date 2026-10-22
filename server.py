"""RaceBrick_Sat2Pixel HTTP service.

ESP32 posts a 4-corner GNSS bounding box, server returns a 4-color (2-bit)
satellite image. Requests are idempotent on (device_id, seq) so retransmitted
requests are answered from cache without re-fetching tiles.
"""

import io
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import ValidationError

from sat2pixel.mosaic import fetch_satellite
from sat2pixel.quantize import encode_bin, quantize_4color
from sat2pixel import protocol as P

app = FastAPI(title="RaceBrick_Sat2Pixel", version="0.1.0")

# (device_id, seq) -> (expires_at, body, media_type, headers)
_CACHE: dict = {}


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
        expired = [k for k, v in _CACHE.items() if v[0] < time.time()]
        for k in expired:
            _CACHE.pop(k, None)


def _palette_header(palette):
    return ",".join("#{:02X}{:02X}{:02X}".format(*rgb) for rgb in palette)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": "0.1.0"}


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
        return Response(
            content=exc.json(),
            media_type="application/json",
            status_code=422,
        )

    key = (req.device_id, req.seq)
    cached = _cache_get(key)
    if cached is not None:
        _, cbody, ctype, cheaders = cached
        return Response(content=cbody, media_type=ctype, headers=cheaders)

    corners = [c.dict() for c in req.corners]
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
