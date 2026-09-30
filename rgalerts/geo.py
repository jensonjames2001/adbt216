"""Pure geometry helpers: great-circle distance, slippy-map tiles, MVT pixels.

No I/O, no network, no third-party packages. Everything here is plain maths
so the probes and the live service share one set of formulas.

Conventions
-----------
* Latitude/longitude are decimal degrees (WGS84). Functions take
  ``(lat, lon)`` in that order EXCEPT the tile helpers and polylines, which
  follow the GeoJSON / MVT habit of ``(lon, lat)`` -- the parameter names say
  which is which.
* Slippy-map tiles (OpenStreetMap / TomTom scheme): ``n = 2**zoom``,
  ``x`` grows eastward from lon -180, ``y`` grows SOUTHWARD from lat ~85.05.
* Pixels inside a Mapbox Vector Tile: ``extent`` units per tile side
  (TomTom uses 4096), origin at the tile's TOP-LEFT corner, ``px`` grows
  eastward and ``py`` grows southward (the MVT specification).
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

EARTH_RADIUS_M = 6371008.8   # mean Earth radius, metres (IUGG)
METRES_PER_MILE = 1609.344
MAX_LAT = 85.05112878        # Web Mercator limit

__all__ = [
    "EARTH_RADIUS_M",
    "METRES_PER_MILE",
    "haversine_m",
    "miles",
    "bearing_deg",
    "lonlat_to_tile",
    "tile_bounds",
    "tiles_for_box",
    "pixel_to_lonlat",
    "point_to_polyline_m",
    "point_in_box",
]


# ---------------------------------------------------------------------------
# Distances and bearings
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2.0) ** 2
    # clamp guards against rounding pushing sqrt(h) a hair above 1
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def miles(m: float) -> float:
    """Metres -> statute miles."""
    return m / METRES_PER_MILE


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing in degrees, 0 <= result < 360, from point 1 to point 2.

    0 = north, 90 = east, 180 = south, 270 = west. Two identical points give 0.
    """
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    b = math.degrees(math.atan2(y, x))
    b = b % 360.0
    if b >= 360.0:  # -0.0 % 360 can be 360.0 on some platforms
        b -= 360.0
    return b


# ---------------------------------------------------------------------------
# Slippy-map tiles
# ---------------------------------------------------------------------------

def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """(lon, lat) -> (x, y) tile indices at ``zoom``.

    Latitude is clamped to the Web Mercator limit and the result is clamped
    to ``0 .. 2**zoom - 1`` so lon = 180 or lat = -85.06 never yields an
    index one past the last tile.
    """
    n = 1 << int(zoom)
    lat = max(-MAX_LAT, min(MAX_LAT, float(lat)))
    lat_rad = math.radians(lat)
    x = math.floor((float(lon) + 180.0) / 360.0 * n)
    y = math.floor((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, int(x)))
    y = max(0, min(n - 1, int(y)))
    return x, y


def _tile_edge_lat(zoom: int, y: float) -> float:
    n = 1 << int(zoom)
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))


def _tile_edge_lon(zoom: int, x: float) -> float:
    n = 1 << int(zoom)
    return x / n * 360.0 - 180.0


def tile_bounds(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) in degrees of tile (zoom, x, y)."""
    west = _tile_edge_lon(zoom, x)
    east = _tile_edge_lon(zoom, x + 1)
    north = _tile_edge_lat(zoom, y)
    south = _tile_edge_lat(zoom, y + 1)
    return west, south, east, north


def tiles_for_box(box: Any, zoom: int) -> list[tuple[int, int, int]]:
    """Sorted list of (zoom, x, y) tiles that cover ``box``.

    ``box`` is anything with ``.west .south .east .north`` attributes, e.g.
    ``rgalerts.config.Box``. Tile indices come from ``floor``, so a box edge
    that lands exactly on a tile edge is harmless but not symmetric: an EAST
    or SOUTH edge on a tile line includes one extra tile on the far side,
    while a WEST or NORTH edge on a tile line does not.
    """
    zoom = int(zoom)
    x0, y0 = lonlat_to_tile(box.west, box.north, zoom)   # north-west corner
    x1, y1 = lonlat_to_tile(box.east, box.south, zoom)   # south-east corner
    if x1 < x0 or y1 < y0:
        raise ValueError("box is inverted: west<east and south<north are required")
    return [(zoom, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def pixel_to_lonlat(zoom: int, x: int, y: int, px: float, py: float,
                    extent: int = 4096) -> tuple[float, float]:
    """MVT pixel -> (lon, lat). Origin is the tile's TOP-LEFT corner; ``py``
    grows southward (the MVT spec). Pixels a little outside ``0..extent``
    (buffer geometry) are converted as-is, never clamped.

    Sanity: tile (10, 511, 340) pixel (0, 0) -> lon -0.3515625, lat ~51.618.
    """
    n = 1 << int(zoom)
    lon = (x + px / extent) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + py / extent) / n))))
    return lon, lat


# ---------------------------------------------------------------------------
# Point / polyline / box
# ---------------------------------------------------------------------------

def _local_xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Equirectangular projection about (lat0, lon0), in metres."""
    k = math.cos(math.radians(lat0))
    dx = math.radians(lon - lon0) * k * EARTH_RADIUS_M
    dy = math.radians(lat - lat0) * EARTH_RADIUS_M
    return dx, dy


def _point_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Planar distance from P to segment AB."""
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    denom = abx * abx + aby * aby
    if denom == 0.0:
        return math.hypot(apx, apy)
    t = (apx * abx + apy * aby) / denom
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * abx, ay + t * aby
    return math.hypot(px - cx, py - cy)


def point_to_polyline_m(lat: float, lon: float, line: Sequence[Sequence[float]]) -> float:
    """Metres from (lat, lon) to the nearest segment of ``line``.

    ``line`` is ``[(lon, lat), ...]`` (GeoJSON order, as Report.line).
    Uses a local equirectangular projection centred on the point, which is
    accurate to well under 1 % at the ~30 km scale of our box. An empty
    line gives ``inf``; a one-point line gives the distance to that point.
    """
    pts = [(_local_xy(lat, lon, float(p[1]), float(p[0]))) for p in line]
    if not pts:
        return math.inf
    if len(pts) == 1:
        return math.hypot(pts[0][0], pts[0][1])
    best = math.inf
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        d = _point_to_segment(0.0, 0.0, ax, ay, bx, by)
        if d < best:
            best = d
    return best


def point_in_box(lat: float, lon: float, box: Any) -> bool:
    """True when (lat, lon) is inside ``box`` (inclusive edges).
    ``box`` is anything with ``.west .south .east .north``."""
    return box.south <= lat <= box.north and box.west <= lon <= box.east
