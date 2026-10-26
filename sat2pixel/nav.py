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

# Ground-width limits. 8,000 km covers the whole of China with margin at the
# minimum zoom factor used by the device.
MIN_SPAN_M = 20.0
MAX_SPAN_M = 8_000_000.0


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
    return max(MIN_SPAN_M, min(MAX_SPAN_M, float(size_m) / float(zoom)))


def square_corners(cx, cy, size_m):
    return rect_corners(cx, cy, size_m, size_m)


def rect_corners(cx, cy, width_m, height_m):
    half_w = width_m / 2.0
    half_h = height_m / 2.0
    dlat = half_h / 111320.0
    dlng = half_w / (111320.0 * math.cos(math.radians(cy)))
    return [
        {"lng": cx - dlng, "lat": cy - dlat},
        {"lng": cx + dlng, "lat": cy - dlat},
        {"lng": cx + dlng, "lat": cy + dlat},
        {"lng": cx - dlng, "lat": cy + dlat},
    ]


def _build_nav_rect(cx, cy, width_m, width_px, height_px, heading, layer):
    """Rectangular viewport: width_m is the ground width, height follows pixels."""
    height_m = width_m * height_px / width_px
    rotate = (heading % 360.0) != 0.0
    pixels = min(_MAX_FETCH_PX, max(16, int(round(width_px * 2))))
    zoom = zoom_for_size(cy, width_m, pixels)

    if rotate:
        # Fetch a square large enough for the rotated viewport, then crop the
        # requested rectangle out of its centre.
        side = max(width_m, height_m) * 1.5
        corners = rect_corners(cx, cy, side, side)
        image, used_zoom, _ = fetch_satellite(corners, zoom, layer)
        image = image.rotate(heading, resample=Image.Resampling.BICUBIC, expand=False)
    else:
        corners = rect_corners(cx, cy, width_m, height_m)
        image, used_zoom, _ = fetch_satellite(corners, zoom, layer)

    mpp = ground_mpp(cy, used_zoom)
    crop_w = min(image.width, max(1, int(round(width_m / mpp))))
    crop_h = min(image.height, max(1, int(round(height_m / mpp))))
    left = (image.width - crop_w) // 2
    top = (image.height - crop_h) // 2
    image = image.crop((left, top, left + crop_w, top + crop_h))
    return image.resize((width_px, height_px), Image.Resampling.LANCZOS), used_zoom


def build_nav_image(cx, cy, size_m, size_px=240, heading=0.0, layer="road", height_px=None):
    """Fetch the area centred on (cx, cy) and return an RGB image.

    heading (degrees clockwise from north) rotates the map so that direction is
    up. Square output keeps the original 1.5x oversample + centre-crop path;
    rectangular output (e.g. 320x240 fullscreen) uses a dedicated viewport path.
    """
    width_px = int(size_px)
    height_px = width_px if height_px is None else int(height_px)
    if width_px != height_px:
        return _build_nav_rect(cx, cy, float(size_m), width_px, height_px, heading, layer)

    rotate = (heading % 360.0) != 0.0
    ratio = 1.5 if rotate else 1.0
    fetch_size = size_m * ratio
    # 2x supersample for a crisp downsample, but never fetch a monster image.
    pixels = min(_MAX_FETCH_PX, max(16, int(round(width_px * ratio * 2))))
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

    return downsample(image, width_px, "lanczos", 0.4), used_zoom
