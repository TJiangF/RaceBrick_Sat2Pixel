"""Wire protocol definitions: request models, error codes, retransmission policy."""

from pydantic import BaseModel, Field

ERR_INVALID_JSON = "INVALID_JSON"
ERR_INVALID_CORNERS = "INVALID_CORNERS"
ERR_TILE_FETCH = "TILE_FETCH_FAILED"
ERR_INTERNAL = "INTERNAL"

# Retransmission policy
REQUEST_TIMEOUT_S = 5
MAX_RETRIES = 3
BACKOFF_S = [1, 2, 4]
DEDUP_TTL_S = 600


class Corner(BaseModel):
    lng: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)


class SatelliteRequest(BaseModel):
    """Scenario 1: a series of track points -> outermost 4 corners -> image."""

    device_id: str = Field(..., min_length=1, max_length=64)
    seq: int = Field(..., ge=0, le=2**32 - 1)
    corners: list[Corner] = Field(..., min_length=4, max_length=4)
    max_size: int = Field(240, ge=16, le=1024)
    format: str = Field("rgb565", pattern="^(rgb565|png|jpeg)$")
    coord: str = Field("gcj02", pattern="^(gcj02|wgs84)$")
    layer: str = Field("satellite", pattern="^(satellite|road|amap_road|tdt_satellite)$")


class NavRequest(BaseModel):
    """Scenario 2: current position -> real-time compressed navigation image."""

    device_id: str = Field(..., min_length=1, max_length=64)
    seq: int = Field(..., ge=0, le=2**32 - 1)
    lng: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)
    size_m: float = Field(300, ge=20, le=10000)   # base ground width centred on position
    heading: float = Field(0.0, ge=-360, le=360)  # degrees clockwise from north; 0 = north-up
    size: int = Field(240, ge=16, le=512)         # output pixels (square, variable)
    width: int | None = Field(None, ge=16, le=512)   # optional rectangular output
    height: int | None = Field(None, ge=16, le=512)
    zoom: float = Field(1.0, ge=0.25, le=8.0)     # >1 zoom in, <1 zoom out
    format: str = Field("pal16", pattern="^(pal16|rgb565|png|jpeg)$")
    coord: str = Field("gcj02", pattern="^(gcj02|wgs84)$")
    layer: str = Field("road", pattern="^(satellite|road|amap_road|tdt_satellite)$")
    style: str = Field("pixel", pattern="^(pixel|original)$")
    theme: str = Field("gameboy", pattern="^(original|vice|gta|cyber|paper|minimal|blueprint|gameboy|radar|compass|satellite_alt)$")
    process: bool = True
    sat_opacity: int = Field(100, ge=0, le=100)
    mode: str = Field("auto", pattern="^(auto|road|satellite|blend)$")
