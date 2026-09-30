"""Road geometry index: "is this point on the M25, and which way does that
carriageway run?"

Reads data/roads.json (written by scripts/fetch_junctions.py from
OpenStreetMap). Each way is a polyline of (lon, lat) vertices in the OSM
node order; when ``oneway`` is True that order is the direction of travel,
so ``segment_bearing`` tells a later phase which carriageway a report is on.

Lookups are a linear scan over every way with a bounding-box prefilter,
which is plenty for a few hundred ways.

Attribution: Map data (c) OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

_EARTH_RADIUS_M = 6371008.8

try:  # rgalerts.geo is written by another agent; use it when it is there
    from rgalerts.geo import point_to_polyline_m, bearing_deg
except ImportError:  # pragma: no cover - only when geo.py is missing
    def _local_xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
        k = math.cos(math.radians(lat0))
        return (math.radians(lon - lon0) * k * _EARTH_RADIUS_M,
                math.radians(lat - lat0) * _EARTH_RADIUS_M)

    def _point_to_polyline_m(lat: float, lon: float, line: Sequence[Sequence[float]]) -> float:
        """Metres from (lat, lon) to the nearest segment of line [(lon, lat), ...]."""
        pts = [_local_xy(lat, lon, float(p[1]), float(p[0])) for p in line]
        if not pts:
            return math.inf
        if len(pts) == 1:
            return math.hypot(*pts[0])
        return min(_segment_distance(0.0, 0.0, a, b) for a, b in zip(pts, pts[1:]))

    def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dlam = math.radians(lon2 - lon1)
        y = math.sin(dlam) * math.cos(p2)
        x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    point_to_polyline_m = _point_to_polyline_m


def _segment_distance(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Planar distance from P to segment AB."""
    ax, ay = a
    bx, by = b
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    denom = abx * abx + aby * aby
    if denom == 0.0:
        return math.hypot(apx, apy)
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / denom))
    return math.hypot(px - (ax + t * abx), py - (ay + t * aby))


def _nearest_segment_index(lat: float, lon: float, coords: Sequence[Sequence[float]]) -> int:
    """Index i of the segment coords[i]..coords[i+1] closest to (lat, lon)."""
    k = math.cos(math.radians(lat))
    pts = [(math.radians(float(p[0]) - lon) * k * _EARTH_RADIUS_M,
            math.radians(float(p[1]) - lat) * _EARTH_RADIUS_M) for p in coords]
    best_i, best_d = 0, math.inf
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        d = _segment_distance(0.0, 0.0, a, b)
        if d < best_d:
            best_i, best_d = i, d
    return best_i


@dataclass(frozen=True, slots=True)
class Way:
    road: str                       # "M25", "A316" ... (upper case)
    osm_id: int
    oneway: Optional[bool]          # True: coords run in the direction of travel
    highway: Optional[str]          # motorway | trunk | primary
    name: Optional[str]
    lanes: Optional[int]
    coords: tuple[tuple[float, float], ...]   # ((lon, lat), ...)
    bbox: tuple[float, float, float, float]   # west, south, east, north

    @property
    def length_m(self) -> float:
        return sum(_haversine(a, b) for a, b in zip(self.coords, self.coords[1:]))


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2.0) ** 2)
    return 2.0 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _make_way(road: str, raw: dict) -> Optional[Way]:
    coords = tuple((float(c[0]), float(c[1])) for c in (raw.get("coords") or []))
    if len(coords) < 2:
        return None
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    lanes = raw.get("lanes")
    return Way(
        road=road.upper(),
        osm_id=int(raw.get("osm_id") or 0),
        oneway=raw.get("oneway") if isinstance(raw.get("oneway"), bool) else None,
        highway=raw.get("highway"),
        name=raw.get("name"),
        lanes=int(lanes) if isinstance(lanes, int) and not isinstance(lanes, bool) else None,
        coords=coords,
        bbox=(min(lons), min(lats), max(lons), max(lats)),
    )


class RoadIndex:
    """Nearest-road lookup over the configured roads' OSM ways."""

    def __init__(self, ways: Iterable[Way], attribution: Optional[str] = None,
                 generated_at: Optional[str] = None) -> None:
        self.ways: list[Way] = list(ways)
        self.attribution = attribution
        self.generated_at = generated_at

    @classmethod
    def load(cls, path: str | os.PathLike) -> "RoadIndex":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ways: list[Way] = []
        for road, raw_ways in (data.get("roads") or {}).items():
            for raw in raw_ways or []:
                w = _make_way(str(road), raw)
                if w is not None:
                    ways.append(w)
        return cls(ways, data.get("attribution"), data.get("generated_at"))

    def __len__(self) -> int:
        return len(self.ways)

    @property
    def roads(self) -> list[str]:
        return sorted({w.road for w in self.ways})

    def nearest(self, lat: float, lon: float, max_m: float = 100.0,
                roads: Optional[Sequence[str]] = None) -> Optional[dict]:
        """The closest way within ``max_m`` metres of (lat, lon), or None.

        Returns {road, distance_m, osm_id, oneway, segment_bearing, highway,
        name, lanes}. ``segment_bearing`` is the compass bearing (degrees) of
        the nearest segment in the way's node order, which is the direction
        of travel only when ``oneway`` is True. ``roads`` limits the search
        to those road refs.
        """
        lat, lon = float(lat), float(lon)
        max_m = float(max_m)
        wanted = {str(r).upper() for r in roads} if roads is not None else None
        pad_lat = max_m / 111_320.0
        pad_lon = max_m / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
        best: Optional[Way] = None
        best_d = math.inf
        for w in self.ways:
            if wanted is not None and w.road not in wanted:
                continue
            west, south, east, north = w.bbox
            if (lon < west - pad_lon or lon > east + pad_lon
                    or lat < south - pad_lat or lat > north + pad_lat):
                continue
            d = point_to_polyline_m(lat, lon, w.coords)
            if d < best_d:
                best, best_d = w, d
        if best is None or best_d > max_m:
            return None
        i = _nearest_segment_index(lat, lon, best.coords)
        (lon1, lat1), (lon2, lat2) = best.coords[i], best.coords[i + 1]
        return {
            "road": best.road,
            "distance_m": best_d,
            "osm_id": best.osm_id,
            "oneway": best.oneway,
            "segment_bearing": bearing_deg(lat1, lon1, lat2, lon2),
            "highway": best.highway,
            "name": best.name,
            "lanes": best.lanes,
        }

    def on_listed_road(self, lat: float, lon: float, roads: Sequence[str],
                       max_m: float = 100.0) -> Optional[str]:
        """The road ref from ``roads`` that (lat, lon) is within ``max_m`` of,
        or None. Roads not in the list are ignored entirely."""
        hit = self.nearest(lat, lon, max_m=max_m, roads=list(roads))
        return hit["road"] if hit else None
