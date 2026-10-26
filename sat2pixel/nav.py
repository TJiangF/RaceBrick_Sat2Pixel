"""Navigation-view geometry: centre + ground size (+ optional heading) -> image."""

import math

from PIL import Image

from sat2pixel.mosaic import fetch_satellite
from sat2pixel.preview import downsample

# web-mercator ground resolution at the equator for 256px tiles
_EQ_MPP_Z0 = 156543.03392

# Never stitch more than this many pixels across, even for a large output size.
# The device renders 240x240; the cap only protects the server from a huge
# `size` request (e.g. 512 with 1.5x rotation oversampling).
_MAX_FETCH_PX = 1024


def ground_mpp(lat, zoom):
    return _EQ_MPP_Z0 * math.cos(math.radians(lat)) / (2 ** zoom)


def zoom_for_size(lat, size_m, pixels):
    """Smallest zoom whose ground resolution gives at least `pixels` across `size_m`."""
    target_mpp = size_m / pixels
    ideal = math.log2(_EQ_MPP_Z0 * math.cos(math.radians(lat)) / target_mpp)
    return max(2, min(18, int(math.ceil(ideal))))


def effective_span_m(size_m, zoom):
    """Ground width after zoom (>1 zooms in) clamped to the API's legal range."""
    if not zoom or zoom <= 0:
        zoom = 1.0
    return max(20.0, min(10000.0, float(size_m) / float(zoom)))


def square_corners(cx, cy, size_m):
    half = size_m / 2.0
    dlat = half / 111320.0
    dlng = half / (111320.0 * math.cos(math.radians(cy)))
    return [
        {"lng": cx - dlng, "lat": cy - dlat},
        {"lng": cx + dlng, "lat": cy - dlat},
        {"lng": cx + dlng, "lat": cy + dlat},
        {"lng": cx - dlng, "lat": cy + dlat},
    ]


def build_nav_image(cx, cy, size_m, size_px=240, heading=0.0, layer="road"):
    """Fetch the square area centred on (cx, cy) and return a size_px RGB image.

    heading (degrees clockwise from north) rotates the map so that direction is
    up; a 1.5x oversample avoids black corners after rotation, then a centre crop
    keeps exactly size_m ground coverage.
    """
    rotate = (heading % 360.0) != 0.0
    ratio = 1.5 if rotate else 1.0
    fetch_size = size_m * ratio
    # 2x supersample for a crisp downsample, but never fetch a monster image.
    pixels = min(_MAX_FETCH_PX, max(16, int(round(size_px * ratio * 2))))
    zoom = zoom_for_size(cy, fetch_size, pixels)
    corners = square_corners(cx, cy, fetch_size)
    image, used_zoom, _ = fetch_satellite(corners, zoom, layer)

    if rotate:
        image = image.rotate(heading, resample=Image.Resampling.BICUBIC, expand=False)
        f = image.width
        crop = int(round(f / ratio))
        left = (f - crop) // 2
        top = (f - crop) // 2
        image = image.crop((left, top, left + crop, top + crop))

    return downsample(image, size_px, "lanczos", 0.4), used_zoom
