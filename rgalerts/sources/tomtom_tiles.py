"""TomTom Traffic Incidents *vector tiles* (legacy non-Orbis endpoint): URL
building and tile decoding. Parsing only -- no network in this module.

Endpoint (docs: developer.tomtom.com/traffic-api, "Vector Incident Tiles")::

    GET https://api.tomtom.com/traffic/map/4/tile/incidents/{zoom}/{x}/{y}.pbf
        ?key=KEY&tags=[icon_category,description,road_type,...]

* Zoom 0..22, slippy-map x/y (see rgalerts.geo).
* Two layers: "Traffic incident POI" (points) and "Traffic incident flow"
  (lines). The prose docs say "Traffic incidents POI/flow" so we match the
  layer name loosely: case-insensitive, contains "poi" or "flow".
* Extent 4096, origin TOP-LEFT (MVT specification).
* Per-feature tags: icon_category_0, description_0, icon_category_1, ...;
  id (only when requested; same id as Incident Details); cluster_id and
  cluster_size mark a cluster of POIs (a cluster carries a plain
  icon_category, 13 when its members differ); clustered (int); poi_type
  start_poi|standalone_poi; magnitude 0..4; delay (s); road_type;
  number_of_reports; probability_of_occurrence; last_report_time; end_date.
* An incident is a breakdown when ANY icon_category_N == 14.
* Response headers carry an ETag; send If-None-Match, 304 = unchanged.
* 403 / 429 mean "quota gone for this month", not a transient error.

Which way is up? (the y-convention question the Phase 0 probe must settle)
--------------------------------------------------------------------------
A Mapbox Vector Tile stores integer pixel coordinates. Per the MVT spec the
origin is the tile's TOP-LEFT corner and py grows southward. The library
``mapbox_vector_tile.decode`` by default (``y_coord_down=False``) FLIPS py to
``extent - py`` (bottom-left origin); with ``default_options={"y_coord_down":
True}`` it returns the stored integers unchanged. ``decode_tile`` undoes the
library's flip, so the two settings give IDENTICAL lon/lat -- the setting only
changes how we get at the stored integers, never what they mean.

What could differ is what the stored integers MEAN. Exactly two hypotheses:

  Hypothesis A ("spec", ``raw_flip=False``, the default):
      the stored py is measured from the tile's TOP edge (MVT spec).
      lon/lat = geo.pixel_to_lonlat(zoom, x, y, px, py_stored).
  Hypothesis B ("mirrored", ``raw_flip=True`` or ``mirror_coords()``):
      the stored py is measured from the tile's BOTTOM edge, so the
      top-origin pixel is ``extent - py_stored``.
      lon/lat = geo.pixel_to_lonlat(zoom, x, y, px, extent - py_stored).

Only one of A/B lands decoded points on the carriageway. The probe decodes
real tiles both ways and measures the distance from each point to the OSM
road geometry; the interpretation with the small distances wins. A is what
the spec says and is expected to win; B exists so the check is a
measurement, not an assumption. ``TileFeature.pixel_coords`` always holds
the stored integers (hypothesis-independent) for that check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

import mapbox_vector_tile

from rgalerts import geo

__all__ = [
    "BASE_URL",
    "TAGS",
    "TAGS_PARAM",
    "BREAKDOWN_CATEGORY",
    "CLUSTER_MIXED_CATEGORY",
    "CATEGORY_NAMES",
    "TileFeature",
    "check_tile",
    "tile_url",
    "tile_url_for_log",
    "layer_kind",
    "decode_tile",
    "mirror_coords",
    "mirror_lat",
    "is_breakdown",
    "is_breakdown_cluster",
    "category_name",
    "breakdown_ids",
    "dedupe_by_id",
    "summarize",
]

BASE_URL = "https://api.tomtom.com/traffic/map/4/tile/incidents"

# The tags we ask for. The API wants the literal bracketed list; tile_url()
# URL-encodes it.
TAGS: list[str] = [
    "icon_category",
    "description",
    "road_type",
    "magnitude",
    "delay",
    "id",
    "last_report_time",
    "number_of_reports",
    "probability_of_occurrence",
    "road_category",
]
TAGS_PARAM = "[" + ",".join(TAGS) + "]"

BREAKDOWN_CATEGORY = 14
CLUSTER_MIXED_CATEGORY = 13   # a cluster whose members have different categories

# Icon categories from the TomTom documentation (the same table is used by
# Incident Details). 12 and 13 are not in the documented table; 13 is what
# a mixed cluster carries.
CATEGORY_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "Accident",
    2: "Fog",
    3: "Dangerous Conditions",
    4: "Rain",
    5: "Ice",
    6: "Jam",
    7: "Lane Closed",
    8: "Road Closed",
    9: "Road Works",
    10: "Wind",
    11: "Flooding",
    13: "Mixed cluster",
    14: "Broken Down Vehicle",
}

_ICON_RE = re.compile(r"^icon_category_(\d+)$")
_DESC_RE = re.compile(r"^description_(\d+)$")


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

MIN_ZOOM = 0
MAX_ZOOM = 22


def check_tile(zoom: int, x: int, y: int) -> tuple[int, int, int]:
    """Validate a tile address and return it as ints.

    TomTom accepts zoom 0..22; x and y must be 0 .. 2**zoom - 1. Raising here
    is cheaper than spending one of the month's counted requests on a 400.
    """
    try:
        z, xi, yi = int(zoom), int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"tile address must be integers, got zoom={zoom!r} x={x!r} y={y!r}") from exc
    if not MIN_ZOOM <= z <= MAX_ZOOM:
        raise ValueError(f"zoom {z} is outside TomTom's range {MIN_ZOOM}..{MAX_ZOOM}")
    n = 1 << z
    if not (0 <= xi < n and 0 <= yi < n):
        raise ValueError(f"tile x={xi} y={yi} is outside 0..{n - 1} at zoom {z}")
    return z, xi, yi


def tile_url(zoom: int, x: int, y: int, key: str) -> str:
    """Full request URL including the API key. Never log this; use
    tile_url_for_log() (or rgalerts.config.redact) for anything saved.
    Raises ValueError for an empty key or a tile address outside the
    zoom/x/y range (see check_tile)."""
    if not key:
        raise ValueError("TomTom API key is empty")
    z, xi, yi = check_tile(zoom, x, y)
    return f"{BASE_URL}/{z}/{xi}/{yi}.pbf?" + urlencode(
        [("key", key), ("tags", TAGS_PARAM)]
    )


def tile_url_for_log(zoom: int, x: int, y: int) -> str:
    """The same URL with the key left out entirely (safe for logs/fixtures)."""
    z, xi, yi = check_tile(zoom, x, y)
    return f"{BASE_URL}/{z}/{xi}/{yi}.pbf?" + urlencode([("tags", TAGS_PARAM)])


# ---------------------------------------------------------------------------
# Feature model
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class TileFeature:
    layer: str                          # "poi" | "flow" | "other"
    layer_name: str                     # raw layer name from the tile
    is_cluster: bool
    cluster_id: Optional[int]
    cluster_size: Optional[int]
    incident_id: Optional[str]          # the "id" tag, or None when absent
    icon_categories: list[int]          # icon_category_0.. in index order; a cluster's plain icon_category
    descriptions: list[str]             # description_0.. in index order
    magnitude: Optional[int]
    delay: Optional[int]
    road_type: Optional[str]
    number_of_reports: Optional[int]
    probability_of_occurrence: Optional[str]
    last_report_time: Optional[str]
    end_date: Optional[str]
    poi_type: Optional[str]
    geometry_type: str                  # Point | MultiPoint | LineString | MultiLineString | ...
    coords: list[tuple[float, float]]   # (lon, lat); one point for a POI, the polyline for flow
    pixel_coords: list[tuple[int, int]] # the integers STORED in the tile (top-left origin per spec)
    props: dict = field(default_factory=dict)          # raw tag dict (copy)
    # Extras that later phases need but the brief did not list:
    coord_parts: list[list[tuple[float, float]]] = field(default_factory=list)  # per part (MultiLineString)
    tile: tuple[int, int, int] = (0, 0, 0)             # (zoom, x, y) this came from
    extent: int = 4096
    clustered: Optional[int] = None

    @property
    def lon(self) -> Optional[float]:
        return self.coords[0][0] if self.coords else None

    @property
    def lat(self) -> Optional[float]:
        return self.coords[0][1] if self.coords else None


# ---------------------------------------------------------------------------
# Small tolerant converters (tile tag values may be int, float or str)
# ---------------------------------------------------------------------------

def _as_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
                return int(f) if f.is_integer() else None
            except ValueError:
                return None
    return None


def _as_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def layer_kind(name: str) -> str:
    """Loose layer classification: "poi", "flow" or "other"."""
    n = (name or "").lower()
    if "poi" in n:
        return "poi"
    if "flow" in n:
        return "flow"
    return "other"


def _indexed(props: dict, rx: re.Pattern) -> list[Any]:
    found: list[tuple[int, Any]] = []
    for k, v in props.items():
        m = rx.match(k)
        if m:
            found.append((int(m.group(1)), v))
    found.sort(key=lambda kv: kv[0])
    return [v for _, v in found]


def _pairs(coordinates: Any, gtype: str) -> list[list[tuple[float, float]]]:
    """GeoJSON-like coordinates -> list of parts, each a list of (x, y)."""
    if gtype == "Point":
        return [[(coordinates[0], coordinates[1])]]
    if gtype in ("MultiPoint", "LineString"):
        return [[(c[0], c[1]) for c in coordinates]]
    if gtype in ("MultiLineString", "Polygon"):
        return [[(c[0], c[1]) for c in part] for part in coordinates]
    if gtype == "MultiPolygon":
        return [[(c[0], c[1]) for c in ring] for poly in coordinates for ring in poly]
    # Unknown shape: dig out every [x, y] pair we can find, flat.
    out: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(n, (int, float)) for n in node[:2]):
                out.append((node[0], node[1]))
            else:
                for n in node:
                    walk(n)

    walk(coordinates)
    return [out] if out else []


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def decode_tile(pbf_bytes: bytes, zoom: int, x: int, y: int, *,
                y_coord_down: bool = True, raw_flip: bool = False) -> list[TileFeature]:
    """Decode one incident tile into TileFeatures with lon/lat coordinates.

    y_coord_down:
        Passed to mapbox_vector_tile.decode. True = the library returns the
        stored integers as-is (top-left origin). False = the library returns
        ``extent - py`` (bottom-left origin); we undo that before converting,
        so BOTH values give the same lon/lat. It exists so the probe can show
        that the two library settings agree once normalised.
    raw_flip:
        False = hypothesis A (stored py measured from the TOP edge, MVT spec).
        True  = hypothesis B (stored py measured from the BOTTOM edge), i.e.
        the mirrored reading. See the module docstring. ``pixel_coords`` is
        the stored integers either way.

    Raises ValueError when the bytes are not a vector tile (an HTML error
    page, a truncated download, a JSON error body). An empty body decodes
    to an empty list, as does a tile with no features.
    """
    if not isinstance(pbf_bytes, (bytes, bytearray)):
        raise ValueError(f"decode_tile wants bytes, got {type(pbf_bytes).__name__}")
    try:
        decoded = mapbox_vector_tile.decode(bytes(pbf_bytes),
                                            default_options={"y_coord_down": bool(y_coord_down)})
    except Exception as exc:  # google.protobuf DecodeError or anything else the library raises
        raise ValueError(
            f"not a TomTom vector tile ({len(pbf_bytes)} bytes, starts {bytes(pbf_bytes[:16])!r})"
        ) from exc
    out: list[TileFeature] = []
    for layer_name, layer in decoded.items():
        extent = int(layer.get("extent") or 4096)
        kind = layer_kind(layer_name)
        for feat in layer.get("features", []):
            props = dict(feat.get("properties") or {})
            geom = feat.get("geometry") or {}
            gtype = str(geom.get("type") or "Unknown")
            parts_raw = _pairs(geom.get("coordinates", []), gtype)

            pixel_parts: list[list[tuple[int, int]]] = []
            for part in parts_raw:
                pp: list[tuple[int, int]] = []
                for px, py in part:
                    py_stored = py if y_coord_down else extent - py
                    pp.append((int(round(px)), int(round(py_stored))))
                pixel_parts.append(pp)

            coord_parts: list[list[tuple[float, float]]] = []
            for pp in pixel_parts:
                cp: list[tuple[float, float]] = []
                for px, py_stored in pp:
                    py_top = (extent - py_stored) if raw_flip else py_stored
                    cp.append(geo.pixel_to_lonlat(zoom, x, y, px, py_top, extent))
                coord_parts.append(cp)

            icon_cats = [c for c in (_as_int(v) for v in _indexed(props, _ICON_RE)) if c is not None]
            if not icon_cats and "icon_category" in props:
                c = _as_int(props.get("icon_category"))
                if c is not None:
                    icon_cats = [c]
            descs = [d for d in (_as_str(v) for v in _indexed(props, _DESC_RE)) if d is not None]
            if not descs and "description" in props:
                d = _as_str(props.get("description"))
                if d is not None:
                    descs = [d]

            cluster_id = _as_int(props.get("cluster_id"))
            cluster_size = _as_int(props.get("cluster_size"))
            is_cluster = cluster_id is not None or (cluster_size is not None and cluster_size > 1)

            out.append(TileFeature(
                layer=kind,
                layer_name=str(layer_name),
                is_cluster=is_cluster,
                cluster_id=cluster_id,
                cluster_size=cluster_size,
                incident_id=_as_str(props.get("id")),
                icon_categories=icon_cats,
                descriptions=descs,
                magnitude=_as_int(props.get("magnitude")),
                delay=_as_int(props.get("delay")),
                road_type=_as_str(props.get("road_type")),
                number_of_reports=_as_int(props.get("number_of_reports")),
                probability_of_occurrence=_as_str(props.get("probability_of_occurrence")),
                last_report_time=_as_str(props.get("last_report_time")),
                end_date=_as_str(props.get("end_date")),
                poi_type=_as_str(props.get("poi_type")),
                geometry_type=gtype,
                coords=[c for part in coord_parts for c in part],
                pixel_coords=[p for part in pixel_parts for p in part],
                props=props,
                coord_parts=coord_parts,
                tile=(int(zoom), int(x), int(y)),
                extent=extent,
                clustered=_as_int(props.get("clustered")),
            ))
    return out


def _feature_tile(feature: TileFeature, zoom: Optional[int], x: Optional[int],
                  y: Optional[int]) -> tuple[int, int, int]:
    """The tile a feature came from. zoom/x/y may be omitted (None); when
    given they must match feature.tile, otherwise the latitudes would be
    silently wrong."""
    if feature is None:
        raise ValueError("feature is required")
    if zoom is None and x is None and y is None:
        return feature.tile
    if zoom is None or x is None or y is None:
        raise ValueError("give zoom, x and y together, or none of them")
    given = (int(zoom), int(x), int(y))
    if given != tuple(feature.tile):
        raise ValueError(f"tile {given} does not match the feature's tile {tuple(feature.tile)}")
    return given


def mirror_coords(zoom: Optional[int] = None, x: Optional[int] = None, y: Optional[int] = None,
                  feature: Optional[TileFeature] = None) -> list[tuple[float, float]]:
    """Hypothesis B for one feature: treat each stored py as measured from the
    tile's BOTTOM edge (top-origin pixel = extent - py). Returns (lon, lat)
    per stored pixel. Longitudes are unchanged; only latitudes move.

    The tile is read from ``feature.tile``. ``zoom, x, y`` may be passed as
    well (older call form) but must then equal ``feature.tile``; a mismatch
    raises ValueError instead of returning wrong latitudes.
    """
    z, xi, yi = _feature_tile(feature, zoom, x, y)
    ext = feature.extent or 4096
    return [geo.pixel_to_lonlat(z, xi, yi, px, ext - py, ext) for px, py in feature.pixel_coords]


def mirror_lat(zoom: Optional[int] = None, x: Optional[int] = None, y: Optional[int] = None,
               feature: Optional[TileFeature] = None) -> Optional[float]:
    """Latitude of the feature's first point under hypothesis B (mirrored),
    or None when the feature has no coordinates. Same arguments as
    mirror_coords."""
    m = mirror_coords(zoom, x, y, feature)
    return m[0][1] if m else None


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def is_breakdown(f: TileFeature) -> bool:
    """True when any icon_category_N is 14 and the feature is not a cluster.

    A cluster is never a breakdown here even when its plain icon_category is
    14: a cluster carries no incident id, so its members cannot be looked up
    or de-duplicated. That means a zoom low enough for TomTom to cluster
    POIs HIDES breakdown ids from breakdown_ids(); use is_breakdown_cluster()
    to see how many clusters may be hiding breakdowns, and poll at a zoom
    where they do not appear.
    """
    return (not f.is_cluster) and BREAKDOWN_CATEGORY in f.icon_categories


def is_breakdown_cluster(f: TileFeature) -> bool:
    """True for a cluster that certainly holds breakdowns (icon_category 14:
    every member is a breakdown) or may do (13: members of mixed categories).
    Such clusters have no ids, so their breakdowns are invisible to
    is_breakdown() and breakdown_ids(); the count tells you the zoom is too
    low."""
    return f.is_cluster and (BREAKDOWN_CATEGORY in f.icon_categories
                             or CLUSTER_MIXED_CATEGORY in f.icon_categories)


def category_name(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    return CATEGORY_NAMES.get(int(n), f"unknown category {int(n)}")


def breakdown_ids(features: Iterable[TileFeature]) -> set[str]:
    """Incident ids of breakdown features (features without an id are skipped)."""
    return {f.incident_id for f in features if f.incident_id and is_breakdown(f)}


def dedupe_by_id(features: Iterable[TileFeature]) -> list[TileFeature]:
    """Keep the first feature per incident id (a POI and its flow line share
    an id, and a tile boundary can repeat a feature). Features without an
    id are all kept -- we cannot tell them apart."""
    seen: set[str] = set()
    out: list[TileFeature] = []
    for f in features:
        if f.incident_id is None:
            out.append(f)
            continue
        if f.incident_id in seen:
            continue
        seen.add(f.incident_id)
        out.append(f)
    return out


def summarize(features: Iterable[TileFeature]) -> dict:
    """Counts for the probe's report.

    Keys: features, by_layer, by_geometry (both partitions of ``features``),
    category_mentions (how many features mention each category name; a
    feature with two categories, e.g. Jam + Broken Down Vehicle, is counted
    under both, so the values add up to MORE than ``features``; clusters are
    listed as "<name> (in cluster)"), clusters, cluster_members_total,
    breakdown_features, breakdown_clusters (clusters that certainly or
    possibly hide breakdowns, see is_breakdown_cluster), breakdown_ids,
    unique_ids, features_without_id.
    """
    feats = list(features)
    by_layer = {"poi": 0, "flow": 0, "other": 0}
    category_mentions: dict[str, int] = {}
    by_geometry: dict[str, int] = {}
    clusters = 0
    cluster_members = 0
    without_id = 0
    for f in feats:
        by_layer[f.layer] = by_layer.get(f.layer, 0) + 1
        by_geometry[f.geometry_type] = by_geometry.get(f.geometry_type, 0) + 1
        if f.is_cluster:
            clusters += 1
            cluster_members += f.cluster_size or 0
        if f.incident_id is None:
            without_id += 1
        cats = f.icon_categories or [None]
        for c in sorted(set(cats), key=lambda v: -1 if v is None else v):
            name = category_name(c)
            if f.is_cluster:
                name = f"{name} (in cluster)"
            category_mentions[name] = category_mentions.get(name, 0) + 1
    bd = [f for f in feats if is_breakdown(f)]
    return {
        "features": len(feats),
        "by_layer": by_layer,
        "by_geometry": by_geometry,
        "category_mentions": dict(sorted(category_mentions.items())),
        "clusters": clusters,
        "cluster_members_total": cluster_members,
        "breakdown_features": len(bd),
        "breakdown_clusters": sum(1 for f in feats if is_breakdown_cluster(f)),
        "breakdown_ids": len(breakdown_ids(feats)),
        "unique_ids": len({f.incident_id for f in feats if f.incident_id}),
        "features_without_id": without_id,
    }
