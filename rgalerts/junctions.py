"""Junction lookup and the "J13 (Staines) to J14 (Heathrow T4)" labeller.

Reads data/junctions.json (written by scripts/fetch_junctions.py from
OpenStreetMap). Every junction ref and name here comes straight from the
OSM ``ref`` and ``name`` tags of ``highway=motorway_junction`` nodes:
nothing is invented, and a junction without a name is shown as just "J13".

Grouping: OSM has one node per slip road, so a junction usually appears as
two to four nodes with the same ref. ``junctions_on(road)`` merges them into
one entry per ref at the centroid of its nodes. Nodes with a name but no
ref (e.g. "Heston Services", "Apex Corner") are kept as unnumbered
junctions grouped by name; nodes with neither are ignored.

Junction numbers are shown for motorways only (M25, M4, A1(M) ...). On an
A-road the ``ref`` of a motorway_junction node is normally the number of
the motorway junction its slip road leads to (the A316 node at Sunbury
Cross carries the M3's "1"), and the A316 has no "J1" on any sign, so
A-road entries are grouped and shown by name only.

Direction of travel is NOT decided here. ``between()`` returns a and b in
ascending junction order (4 < 4A < 4B < 5, unnumbered last); Phase 1 orders
them by carriageway direction. The labeller works on straight-line
geometry between junction centroids; the proper fix, ordering junctions
along the carriageway from data/roads.json, is Phase 1's job.

Attribution: Map data (c) OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Optional

try:  # rgalerts.geo is written by another agent; fall back to local maths if absent
    from rgalerts.geo import haversine_m as _haversine_m
except ImportError:  # pragma: no cover - only when geo.py is missing
    _EARTH_RADIUS_M = 6371008.8

    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = p2 - p1
        dlam = math.radians(lon2 - lon1)
        h = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2.0) ** 2
        return 2.0 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


_EARTH_RADIUS = 6371008.8
_UNNUMBERED = 10 ** 9   # sort key number for junctions without a numeric ref
_REF_RE = re.compile(r"^\s*J?\s*(\d+)\s*([A-Za-z]{0,2})\s*$")

# between(): a point closer than this to a junction centroid is "near J4",
# not a "J4 to J4B" span.
AT_JUNCTION_M = 150.0
# between(): with no junction on the far side of the point, closer than this
# is "near X"; further away is "beyond X" (the point is past the last junction
# in the data, and Phase 1 must show distance_a_m with it).
NEAR_M = 500.0
# between(): the second junction must lie within 60 degrees of the line from
# the nearest junction through the point (cos > 0.5) ...
_MIN_COS = 0.5
# ... and roughly on the way there: d(near,P) + d(P,cand) <= 1.2 * d(near,cand);
# the same corridor test rejects a candidate that skips another junction.
_CORRIDOR = 1.2


def is_motorway(road: Optional[str]) -> bool:
    """"M25", "M4", "A1(M)" -> True; "A316", "A4" -> False."""
    r = (road or "").strip().upper()
    return r.startswith("M") or r.endswith("(M)")


def junction_ref_key(ref: Optional[str]) -> tuple[int, str]:
    """Sort key for junction refs: "4A" -> (4, "A"), "13" -> (13, ""), "J4b" -> (4, "B").

    A multi-value ref such as "4;4A" sorts by its first value. Refs that are
    not a number plus optional letter suffix (or None) sort after every
    numbered junction, alphabetically.
    """
    if ref is None:
        return (_UNNUMBERED, "")
    first = str(ref).split(";")[0]
    m = _REF_RE.match(first)
    if not m:
        return (_UNNUMBERED, str(ref).strip().upper())
    return (int(m.group(1)), m.group(2).upper())


def normalise_ref(ref: Optional[str]) -> Optional[str]:
    """"4a" -> "4A", " 13 " -> "13", None/"" -> None. A leading J is dropped.

    OSM multi-value refs keep their ";" separator with each part normalised:
    "4;4a" -> "4;4A" (displayed as "J4/J4A").
    """
    if ref is None:
        return None
    s = str(ref).strip().upper()
    if not s:
        return None
    parts = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        m = _REF_RE.match(part)
        parts.append(f"{int(m.group(1))}{m.group(2).upper()}" if m else part)
    return ";".join(parts) or None


@dataclass(frozen=True, slots=True)
class Junction:
    road: str                 # the road this entry was grouped for, e.g. "M25"
    ref: Optional[str]        # junction number from OSM, e.g. "13", "4A"; None when unnumbered
    name: Optional[str]       # OSM name tag, e.g. "Runnymede"; None when OSM has none
    lat: float                # centroid of the member nodes
    lon: float
    osm_ids: tuple[int, ...]  # member node ids
    n_nodes: int

    @property
    def display(self) -> str:
        """"J13 (Runnymede)", "J13", "J4/J4A", or "Heston Services" (never invented)."""
        ref = "/".join(f"J{p}" for p in self.ref.split(";") if p) if self.ref else ""
        if ref and self.name:
            return f"{ref} ({self.name})"
        if ref:
            return ref
        return self.name or "unknown"

    @property
    def sort_key(self) -> tuple[int, str, str]:
        n, suffix = junction_ref_key(self.ref)
        return (n, suffix, (self.name or "").upper())

    @property
    def numbered(self) -> bool:
        return junction_ref_key(self.ref)[0] != _UNNUMBERED

    def to_dict(self) -> dict:
        return {
            "road": self.road, "ref": self.ref, "name": self.name,
            "lat": self.lat, "lon": self.lon, "osm_ids": list(self.osm_ids),
            "n_nodes": self.n_nodes, "display": self.display,
        }


class Junctions:
    """Junction data for the configured roads, loaded from data/junctions.json."""

    def __init__(self, nodes: list[dict], attribution: Optional[str] = None,
                 generated_at: Optional[str] = None) -> None:
        self._nodes = list(nodes)
        self.attribution = attribution
        self.generated_at = generated_at
        self._by_road: dict[str, list[Junction]] = {}

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Junctions":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data.get("junctions") or [], data.get("attribution"), data.get("generated_at"))

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def roads(self) -> list[str]:
        """Every road ref that has at least one junction node, sorted."""
        seen: set[str] = set()
        for n in self._nodes:
            for r in self._node_roads(n):
                seen.add(r)
        return sorted(seen)

    @staticmethod
    def _node_roads(node: dict) -> list[str]:
        roads = [str(r).upper() for r in (node.get("roads") or []) if r]
        if not roads and node.get("road"):
            roads = [str(node["road"]).upper()]
        return roads

    def junctions_on(self, road: str, include_unnumbered: bool = True) -> list[Junction]:
        """One entry per distinct ref on ``road`` (centroid of its nodes, first
        non-null name), sorted 4 < 4A < 4B < 5, then unnumbered by name.

        On a road that is not a motorway (see ``is_motorway``) the OSM ref is
        not used: entries are grouped by name and have ``ref`` None.
        """
        road = str(road).strip().upper()
        if road not in self._by_road:
            self._by_road[road] = self._group(road)
        js = self._by_road[road]
        if not include_unnumbered:
            js = [j for j in js if j.numbered]
        return list(js)

    def _group(self, road: str) -> list[Junction]:
        use_ref = is_motorway(road)
        groups: dict[str, list[dict]] = {}
        for n in self._nodes:
            if road not in self._node_roads(n):
                continue
            ref = normalise_ref(n.get("ref")) if use_ref else None
            name = (n.get("name") or "").strip() or None
            if ref:
                key = "ref:" + ref
            elif name:
                key = "name:" + name.upper()
            else:
                continue   # no ref, no name: nothing honest to show
            groups.setdefault(key, []).append(n)
        out: list[Junction] = []
        for key, members in groups.items():
            members.sort(key=lambda m: int(m.get("osm_id") or 0))
            lat = sum(float(m["lat"]) for m in members) / len(members)
            lon = sum(float(m["lon"]) for m in members) / len(members)
            ref = key[4:] if key.startswith("ref:") else None
            name = next(((m.get("name") or "").strip() for m in members if (m.get("name") or "").strip()), None)
            out.append(Junction(
                road=road, ref=ref, name=name, lat=lat, lon=lon,
                osm_ids=tuple(int(m["osm_id"]) for m in members), n_nodes=len(members),
            ))
        out.sort(key=lambda j: j.sort_key)
        return out

    def nearest(self, lat: float, lon: float, road: str, n: int = 2,
                include_unnumbered: bool = True) -> list[tuple[Junction, float]]:
        """The ``n`` junctions on ``road`` closest to (lat, lon) as (junction, metres)."""
        ranked = sorted(
            ((j, _haversine_m(lat, lon, j.lat, j.lon)) for j in self.junctions_on(road, include_unnumbered)),
            key=lambda t: t[1],
        )
        return ranked[: max(0, int(n))]

    def between(self, lat: float, lon: float, road: Optional[str],
                include_unnumbered: bool = True) -> dict:
        """Which two junctions a point on ``road`` lies between.

        Returns {"a", "b", "label", "form", "nearest", "distance_a_m",
        "distance_b_m", "road"}. ``form`` is one of:

        * "span":   "J13 (Runnymede) to J14 (Poyle)". ``nearest`` is the
                    closest junction and ``b`` the closest junction on the
                    other side of the point from it (within 60 degrees of the
                    nearest->point line and roughly on the way, in a local
                    flat projection). ``a`` and ``b`` are then given in
                    ascending junction order; the direction of travel is
                    decided later, not here.
        * "near":   "near J13 (Runnymede)". The point is within AT_JUNCTION_M
                    (150 m) of a junction centroid, or within NEAR_M (500 m)
                    of the last junction in the data. ``b`` is None.
        * "beyond": "beyond J2 (Thorpe)". No junction lies on the far side of
                    the point and the nearest is over NEAR_M away: the point
                    is past the last junction in the data (e.g. the M3 west
                    of J2 out to the box edge). ``b`` is None. Phase 1 MUST
                    show ``distance_a_m`` with this form ("5.9 mi beyond J2
                    (Thorpe)"), as the label alone reads like "at J2".
        * None:     label None; the road has no junctions in the data, or
                    ``road`` is None.

        This is straight-line geometry between junction centroids. It is
        right on the mainline, but a point on a spur (M4 J4A Concorde
        Roundabout, J7 Huntercombe Spur) can only be placed "near" its own
        junction. Ordering junctions along the carriageway is Phase 1's job.
        """
        empty = {"a": None, "b": None, "label": None, "form": None, "nearest": None,
                 "distance_a_m": None, "distance_b_m": None, "road": road}
        if not road:
            return empty
        ranked = self.nearest(lat, lon, road, n=10 ** 6, include_unnumbered=include_unnumbered)
        if not ranked:
            return empty
        near, d_near = ranked[0]

        def single(form: str) -> dict:
            return {"a": near, "b": None, "label": f"{form} {near.display}", "form": form,
                    "nearest": near, "distance_a_m": d_near, "distance_b_m": None, "road": road}

        if d_near < AT_JUNCTION_M:
            return single("near")

        # local equirectangular projection about the point (metres)
        k = math.cos(math.radians(lat)) * _EARTH_RADIUS
        ky = _EARTH_RADIUS

        def xy(j: Junction) -> tuple[float, float]:
            return (math.radians(j.lon - lon) * k, math.radians(j.lat - lat) * ky)

        ax, ay = xy(near)
        vx, vy = -ax, -ay                 # nearest -> point
        norm_v = math.hypot(vx, vy)
        other: Optional[tuple[Junction, float]] = None
        for j, d in ranked[1:]:
            cx, cy = xy(j)
            wx, wy = cx - ax, cy - ay     # nearest -> candidate
            norm_w = math.hypot(wx, wy)
            if norm_v == 0.0 or norm_w == 0.0:
                continue
            cos = (vx * wx + vy * wy) / (norm_v * norm_w)
            if cos <= _MIN_COS:
                continue
            d_nj = _haversine_m(near.lat, near.lon, j.lat, j.lon)
            if d_near + d > _CORRIDOR * d_nj:
                continue
            # no skipping: another junction on the way from nearest to j means
            # j is not the neighbour (a point 160 m from the J4 centroid must
            # not become "J4 to J6" because J4B's angle came out as noise)
            if any(c is not near and c is not j
                   and _haversine_m(near.lat, near.lon, c.lat, c.lon)
                   + _haversine_m(c.lat, c.lon, j.lat, j.lon) <= _CORRIDOR * d_nj
                   for c, _ in ranked):
                continue
            other = (j, d)
            break
        if other is None:
            return single("near" if d_near < NEAR_M else "beyond")
        b, d_b = other
        a, d_a = near, d_near
        if b.sort_key < a.sort_key:
            a, d_a, b, d_b = b, d_b, a, d_a
        return {"a": a, "b": b, "label": f"{a.display} to {b.display}", "form": "span",
                "nearest": near, "distance_a_m": d_a, "distance_b_m": d_b, "road": road}
