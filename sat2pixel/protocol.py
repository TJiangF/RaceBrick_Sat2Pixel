"""Wire protocol definitions: request model, error codes, retransmission policy."""

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
    device_id: str = Field(..., min_length=1, max_length=64)
    seq: int = Field(..., ge=0, le=2**32 - 1)
    corners: list[Corner] = Field(..., min_length=4, max_length=4)
    max_size: int = Field(240, ge=16, le=1024)
    format: str = Field("rgb565", pattern="^(rgb565|png|jpeg)$")
