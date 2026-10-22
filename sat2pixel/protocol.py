"""Wire protocol definitions: request model, binary format, error codes."""

from typing import List, Optional

from pydantic import BaseModel, Field

# Binary .bin layout
# offset  size  field
# 0       2     width  (uint16 big-endian)
# 2       2     height (uint16 big-endian)
# 4       12    palette: 4 x RGB888 (12 bytes)
# 16      N     pixels: 2 bits/pixel, row-major, each row padded to a byte
HEADER_SIZE = 16

# Error codes
ERR_OK = "OK"
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
    corners: List[Corner] = Field(..., min_items=4, max_items=4)
    zoom: Optional[int] = Field(None, ge=2, le=19)
    palette: str = Field("adaptive", pattern="^(adaptive|gray4)$")
    format: str = Field("bin", pattern="^(bin|png)$")
