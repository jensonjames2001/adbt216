"""TomTom probe: what do the incident tiles and Incident Details really return
for our box, and which tile zoom should the service poll?

Run from the repo root:

    .venv/bin/python probes/probe_tomtom.py --plan-only              # tile list and request plan, sends nothing
    .venv/bin/python probes/probe_tomtom.py                          # one sample: tiles at zoom 10, 11, 12 + Details
    .venv/bin/python probes/probe_tomtom.py --repeat 3 --interval 600   # three samples ten minutes apart
    .venv/bin/python probes/probe_tomtom.py --keep-sample            # also copy one tile per zoom into fixtures/tomtom/

Needs TOMTOM_API_KEY in .env (not for --plan-only). One sample over the
default box costs 43 tile requests (4 + 9 + 30 at zoom 10/11/12), one
Incident Details request by bbox, one by ids, and one ETag re-request: 46
requests. The probe prints the plan first and refuses to go past
--max-requests (default 150 per run), so `--repeat 4` (184 requests) needs
`--max-requests 184` said out loud, and `--repeat 6` needs 276. The free
tier allows 200,000 tile and 2,500 Details requests a month.

Exit codes: 0 = ran to the end, 1 = a request failed (see findings.md),
3 = TOMTOM_API_KEY missing, 4 = TomTom answered 403 or 429, which means an
invalid key or an exhausted month, and the probe stopped at once.

What it answers (findings.md, in the run folder):
  (a) which zoom never missed a breakdown that Incident Details listed
      (a lower zoom folds nearby incidents into clusters without ids);
  (b) which y-axis convention lands decoded tile points on the carriageway;
  (c) whether an incident keeps its id from one sample to the next;
  (d) what a 403/429 looked like, if one happened;
  (e) request counts by product and the projected monthly cost of polling
      each zoom every 2 minutes.

Where things go: fixtures/tomtom/live/<timestamp>/ holds findings.md,
summary.json and one sub-folder per sample (sample_01, sample_02, ...) with
every tile (<z>_<x>_<y>.body.pbf + .meta.json, key stripped from the URL),
the Details bodies, tiles.json and analysis.json. Breakdown ids per sample
are kept in fixtures/tomtom/live/state.json so id persistence can also be
judged across separate runs. Tiles carry no personal data and no secrets.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import urlencode

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from probes import common  # noqa: E402
from probes.common import (  # noqa: E402
    EXIT_FAILED, EXIT_OK, Findings, RequestBudget, explain_exception, london,
    missing_key, now_utc, redacted_url, save_response, secret, status_line,
    timed_request, write_json,
)
from rgalerts import geo  # noqa: E402
from rgalerts.roads import RoadIndex  # noqa: E402
from rgalerts.sources import tomtom_tiles as tt  # noqa: E402

EXIT_QUOTA = 4   # 403 / 429 from TomTom: invalid key or the month's quota is gone

DETAILS_URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"
FIELDS = ("{incidents{type,geometry{type,coordinates},properties{id,iconCategory,"
          "magnitudeOfDelay,events{description,code,iconCategory},startTime,endTime,"
          "from,to,length,delay,roadNumbers,timeValidity,probabilityOfOccurrence,"
          "numberOfReports,lastReportTime}}}")
LANGUAGE = "en-GB"
DEFAULT_ZOOMS = "10,11,12"
DEFAULT_MAX_REQUESTS = 150
MAX_IDS_PER_GET = 5                 # Incident Details: at most 5 ids per GET
FREE_TILES_PER_MONTH = 200_000
FREE_DETAILS_PER_MONTH = 2_500
DEFAULT_POLL_S = 120
DAYS_PER_MONTH = 31                 # the worst case for a monthly budget

NEAR_ROAD_M = 50.0                  # "on the carriageway" for the y-axis check
FAR_M = 200.0                       # a median above this means "not on our roads"
ROAD_SEARCH_M = 3000.0              # distances beyond this are reported as >= 3000 m
INFORMATIVE_SEP_M = 100.0           # A and B must differ by this much to tell them apart
CONSECUTIVE_FAILURES_TO_STOP = 3    # network failures or 5xx answers in a row before the sample is abandoned
QUOTA_BODY_BYTES = 2000

FIXTURE_DIR = _ROOT / "fixtures" / "tomtom"
DEFAULT_STATE_PATH = FIXTURE_DIR / "live" / "state.json"
MAX_STATE_SAMPLES = 500


class QuotaStop(Exception):
    """TomTom answered 403 or 429: stop everything, do not retry."""

    def __init__(self, what: str, status: int, snippet: str, headers: dict[str, str]) -> None:
        super().__init__(f"{what}: HTTP {status}")
        self.what = what
        self.status = status
        self.snippet = snippet
        self.headers = headers


# --------------------------------------------------------------------------
# Pure helpers (no network)
# --------------------------------------------------------------------------

def parse_zooms(text: str) -> list[int]:
    """'10,11,12' -> [10, 11, 12]; sorted, unique, each 0..22."""
    out: set[int] = set()
    for piece in str(text).replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            z = int(piece)
        except ValueError:
            raise ValueError(f"--zooms must be whole numbers separated by commas, not {piece!r}") from None
        if not 0 <= z <= 22:
            raise ValueError(f"--zooms must be between 0 and 22, not {z}")
        out.add(z)
    if not out:
        raise ValueError("--zooms is empty")
    return sorted(out)


def build_plan(box: Any, zooms: Sequence[int], details_limit: int, etag_check: bool,
               repeat: int) -> dict[str, Any]:
    """Tiles per zoom and the request counts, per sample and for the run."""
    tiles = {int(z): geo.tiles_for_box(box, z) for z in zooms}
    per_sample = {
        "tiles": sum(len(t) for t in tiles.values()),
        "details_bbox": int(details_limit),
        "details_by_ids": 1 if details_limit else 0,
        "etag_recheck": 1 if etag_check else 0,
    }
    per_sample_total = sum(per_sample.values())
    return {
        "tiles": tiles,
        "tiles_per_zoom": {z: len(t) for z, t in tiles.items()},
        "per_sample": per_sample,
        "per_sample_total": per_sample_total,
        "repeat": int(repeat),
        "total": per_sample_total * int(repeat),
        "tile_requests_total": (per_sample["tiles"] + per_sample["etag_recheck"]) * int(repeat),
        "details_requests_total": (per_sample["details_bbox"] + per_sample["details_by_ids"]) * int(repeat),
    }


def polls_per_month(poll_s: float) -> float:
    return DAYS_PER_MONTH * 24 * 3600 / float(poll_s)


def monthly_projection(tiles_per_zoom: dict[int, int], poll_s: float = DEFAULT_POLL_S,
                       budget_tiles: int = FREE_TILES_PER_MONTH,
                       free_tiles: int = FREE_TILES_PER_MONTH) -> list[dict[str, Any]]:
    """Per zoom: tile requests a month when polled every poll_s seconds, the
    share of the free tier that uses, and the fastest poll the configured
    budget allows. Every request counts, including 304 answers."""
    polls = polls_per_month(poll_s)
    rows = []
    for z in sorted(tiles_per_zoom):
        n = int(tiles_per_zoom[z])
        per_month = n * polls
        min_interval_s = n * DAYS_PER_MONTH * 24 * 3600 / float(budget_tiles) if budget_tiles else 0.0
        rows.append({
            "zoom": z,
            "tiles_per_poll": n,
            "polls_per_month": round(polls),
            "requests_per_month": round(per_month),
            "share_of_free_tier": per_month / float(free_tiles) if free_tiles else None,
            "fits_free_tier": per_month <= free_tiles,
            "fastest_poll_allowed_by_budget_s": min_interval_s,
        })
    return rows


def dedupe_per_layer(features: Sequence[tt.TileFeature]) -> tuple[list[tt.TileFeature], int]:
    """Keep one feature per (layer, id). Returns (unique, duplicates seen in a
    DIFFERENT tile of the same zoom, i.e. repeats at tile edges). Features
    without an id (clusters) are all kept: they cannot be told apart."""
    seen: dict[tuple[str, str], tt.TileFeature] = {}
    out: list[tt.TileFeature] = []
    edge_dups = 0
    for f in features:
        if f.incident_id is None:
            out.append(f)
            continue
        k = (f.layer, f.incident_id)
        first = seen.get(k)
        if first is None:
            seen[k] = f
            out.append(f)
        elif first.tile != f.tile:
            edge_dups += 1
    return out, edge_dups


def feature_row(f: tt.TileFeature) -> dict[str, Any]:
    return {
        "id": f.incident_id,
        "layer": f.layer,
        "categories": list(f.icon_categories),
        "description": " / ".join(f.descriptions) if f.descriptions else None,
        "number_of_reports": f.number_of_reports,
        "last_report_time": f.last_report_time,
        "probability": f.probability_of_occurrence,
        "magnitude": f.magnitude,
        "delay": f.delay,
        "road_type": f.road_type,
        "poi_type": f.poi_type,
        "lat": round(f.lat, 6) if f.lat is not None else None,
        "lon": round(f.lon, 6) if f.lon is not None else None,
        "tile": list(f.tile),
        "pixel": list(f.pixel_coords[0]) if f.pixel_coords else None,
    }


def analyse_zoom(zoom: int, features: Sequence[tt.TileFeature]) -> dict[str, Any]:
    """Everything the findings say about one zoom's tiles (already decoded)."""
    unique, edge_dups = dedupe_per_layer(features)
    pois = [f for f in unique if f.layer == "poi"]
    flows = [f for f in unique if f.layer == "flow"]
    others = [f for f in unique if f.layer == "other"]
    clusters = [f for f in pois if f.is_cluster]
    breakdowns = [f for f in pois if tt.is_breakdown(f)]
    # A breakdown that only shows as a flow line (no POI) still counts for the id set.
    ids = tt.breakdown_ids(unique)
    hist = Counter()
    for f in pois:
        if f.is_cluster:
            continue
        for c in sorted(set(f.icon_categories)) or [None]:
            hist[tt.category_name(c)] += 1
    cluster_rows = [{
        "cluster_id": f.cluster_id, "size": f.cluster_size,
        "category": f.icon_categories[0] if f.icon_categories else None,
        "category_name": tt.category_name(f.icon_categories[0] if f.icon_categories else None),
        "lat": round(f.lat, 6) if f.lat is not None else None,
        "lon": round(f.lon, 6) if f.lon is not None else None,
        "tile": list(f.tile),
    } for f in clusters]
    could_hide = sum((r["size"] or 0) for r in cluster_rows
                     if r["category"] in (tt.BREAKDOWN_CATEGORY, tt.CLUSTER_MIXED_CATEGORY))
    return {
        "zoom": int(zoom),
        "features_raw": len(features),
        "features_unique": len(unique),
        "edge_duplicates": edge_dups,
        "poi": len(pois),
        "flow": len(flows),
        "other_layer": len(others),
        "layer_names": sorted({f.layer_name for f in features}),
        "clusters": len(clusters),
        "cluster_rows": cluster_rows,
        "cluster_members_total": sum((f.cluster_size or 0) for f in clusters),
        "cluster_members_that_could_be_breakdowns": could_hide,
        "category_histogram": dict(sorted(hist.items())),
        "breakdown_features": len(breakdowns),
        "breakdown_ids": sorted(ids),
        "breakdowns_without_id": sum(1 for f in breakdowns if f.incident_id is None),
        "breakdown_rows": [feature_row(f) for f in breakdowns],
        "features_without_id": sum(1 for f in unique if f.incident_id is None),
        "unique_features": unique,       # objects, for the y-axis check (not saved)
    }


def first_coordinate(geometry: Any) -> Optional[tuple[float, float]]:
    """(lon, lat) of a GeoJSON Point or LineString, else None."""
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    gtype = geometry.get("type")
    try:
        if gtype == "Point":
            return float(coords[0]), float(coords[1])
        if gtype in ("LineString", "MultiPoint"):
            return float(coords[0][0]), float(coords[0][1])
        if gtype == "MultiLineString":
            return float(coords[0][0][0]), float(coords[0][0][1])
    except (TypeError, IndexError, ValueError):
        return None
    return None


def all_coordinates(geometry: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(n, (int, float)) for n in node[:2]):
                out.append((float(node[0]), float(node[1])))
            else:
                for n in node:
                    walk(n)

    if isinstance(geometry, dict):
        walk(geometry.get("coordinates"))
    return out


def details_rows(data: Any, box: Any, roads: Sequence[str]) -> dict[str, Any]:
    """Rows and counts from an Incident Details body. A null entry (an id that
    no longer exists in a by-ids answer) is kept as None."""
    incidents = data.get("incidents") if isinstance(data, dict) else None
    if not isinstance(incidents, list):
        return {"ok": False, "rows": [], "nulls": 0, "ids": [], "on_roads": 0, "in_box": 0,
                "error": "no 'incidents' list in the body"}
    wanted = {str(r).upper() for r in roads}
    rows: list[Optional[dict[str, Any]]] = []
    nulls = 0
    on_roads = 0
    in_box = 0
    for inc in incidents:
        if inc is None:
            nulls += 1
            rows.append(None)
            continue
        props = inc.get("properties") if isinstance(inc, dict) else None
        props = props if isinstance(props, dict) else {}
        geom = inc.get("geometry") if isinstance(inc, dict) else None
        road_numbers = [str(r) for r in (props.get("roadNumbers") or []) if r is not None]
        events = props.get("events") if isinstance(props.get("events"), list) else []
        coords = all_coordinates(geom)
        inside = any(box.contains(lat, lon) for lon, lat in coords) if box is not None else None
        listed = bool({r.upper() for r in road_numbers} & wanted)
        on_roads += 1 if listed else 0
        in_box += 1 if inside else 0
        rows.append({
            "id": str(props["id"]) if props.get("id") is not None else None,
            "iconCategory": props.get("iconCategory"),
            "roadNumbers": road_numbers,
            "from": props.get("from"),
            "to": props.get("to"),
            "startTime": props.get("startTime"),
            "endTime": props.get("endTime"),
            "lastReportTime": props.get("lastReportTime"),
            "numberOfReports": props.get("numberOfReports"),
            "probabilityOfOccurrence": props.get("probabilityOfOccurrence"),
            "magnitudeOfDelay": props.get("magnitudeOfDelay"),
            "delay": props.get("delay"),
            "length": props.get("length"),
            "timeValidity": props.get("timeValidity"),
            "events": [{"code": e.get("code"), "iconCategory": e.get("iconCategory"),
                        "description": e.get("description")} for e in events if isinstance(e, dict)],
            "geometry": geom.get("type") if isinstance(geom, dict) else None,
            "n_points": len(coords),
            "first": first_coordinate(geom),
            "on_listed_road": listed,
            "in_box": inside,
        })
    ids = [r["id"] for r in rows if r is not None and r["id"]]
    return {"ok": True, "rows": rows, "nulls": nulls, "ids": ids, "on_roads": on_roads,
            "in_box": in_box, "error": None}


def compare_ids(details_ids: set[str], tile_ids: set[str]) -> dict[str, Any]:
    return {
        "details": len(details_ids),
        "tiles": len(tile_ids),
        "missing_from_tiles": sorted(details_ids - tile_ids),
        "extra_in_tiles": sorted(tile_ids - details_ids),
        "common": len(details_ids & tile_ids),
    }


def _nearest_m(index: Optional[RoadIndex], lat: float, lon: float, roads: Sequence[str]) -> float:
    if index is None:
        return ROAD_SEARCH_M
    hit = index.nearest(lat, lon, max_m=ROAD_SEARCH_M, roads=list(roads))
    return float(hit["distance_m"]) if hit else ROAD_SEARCH_M


def convention_rows(features: Sequence[tt.TileFeature], index: Optional[RoadIndex],
                    roads: Sequence[str]) -> list[dict[str, Any]]:
    """Per incident: distance from its first point to the nearest configured
    road under hypothesis A (top-left origin, as decoded) and B (mirrored).
    Clusters are skipped: their point is a centroid, not a place. A POI and
    its flow line share an id and a first point, so one row per id is
    measured (the POI when there is one) rather than weighting that incident
    twice; features without an id are all measured."""
    rows = []
    seen_ids: set[str] = set()
    ordered = [f for f in features if f.layer == "poi"] + [f for f in features if f.layer != "poi"]
    for f in ordered:
        if f.is_cluster or not f.coords or not f.pixel_coords:
            continue
        if f.incident_id is not None:
            if f.incident_id in seen_ids:
                continue
            seen_ids.add(f.incident_id)
        lon_a, lat_a = f.coords[0]
        mirrored = tt.mirror_coords(f.tile[0], f.tile[1], f.tile[2], f)
        lon_b, lat_b = mirrored[0]
        rows.append({
            "id": f.incident_id,
            "layer": f.layer,
            "tile": list(f.tile),
            "pixel": list(f.pixel_coords[0]),
            "a": (round(lat_a, 6), round(lon_a, 6)),
            "b": (round(lat_b, 6), round(lon_b, 6)),
            "separation_m": geo.haversine_m(lat_a, lon_a, lat_b, lon_b),
            "a_m": _nearest_m(index, lat_a, lon_a, roads),
            "b_m": _nearest_m(index, lat_b, lon_b, roads),
        })
    return rows


def _dist_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median_m": None, "within_50_m": 0, "within_200_m": 0}
    return {
        "n": len(values),
        "median_m": float(statistics.median(values)),
        "within_50_m": sum(1 for v in values if v <= NEAR_ROAD_M),
        "within_200_m": sum(1 for v in values if v <= FAR_M),
    }


def summarise_convention(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    informative = [r for r in rows if r["separation_m"] >= INFORMATIVE_SEP_M]
    return {
        "n_features": len(rows),
        "n_informative": len(informative),
        "n_uninformative": len(rows) - len(informative),
        "A": _dist_stats([r["a_m"] for r in informative]),
        "B": _dist_stats([r["b_m"] for r in informative]),
        "A_all": _dist_stats([r["a_m"] for r in rows]),
        "B_all": _dist_stats([r["b_m"] for r in rows]),
    }


def judge_convention(s: dict[str, Any]) -> tuple[str, str]:
    """('A' | 'B' | 'neither' | 'unclear' | 'untested', one-line reason)."""
    if s["n_informative"] == 0:
        if s["n_features"] == 0:
            return "untested", "no decoded features at this zoom, so nothing to measure"
        return "untested", (f"{s['n_features']} feature(s) sit so close to the middle row of their tile "
                            f"that A and B differ by under {INFORMATIVE_SEP_M:.0f} m; nothing to tell apart")
    a, b = s["A"], s["B"]
    ma, mb = a["median_m"], b["median_m"]
    ca, cb = a["within_50_m"], b["within_50_m"]
    n = s["n_informative"]
    detail = (f"over {n} informative feature(s): median {ma:.0f} m (A) vs {mb:.0f} m (B); "
              f"within 50 m of a configured road: {ca} (A) vs {cb} (B)")
    if ma <= FAR_M and (mb > FAR_M or ma * 2 < mb) and ca >= cb:
        return "A", f"hypothesis A (top-left origin, as decode_tile implements it) lands on the carriageway: {detail}"
    if mb <= FAR_M and (ma > FAR_M or mb * 2 < ma) and cb >= ca:
        return "B", f"hypothesis B (mirrored) lands on the carriageway: {detail}"
    if ma > FAR_M and mb > FAR_M:
        if ca >= 3 and ca >= 3 * cb:
            return "A", (f"both medians are over {FAR_M:.0f} m (most incidents in these tiles are on roads "
                         f"outside the configured list), but the within-50 m counts decide for A: {detail}")
        if cb >= 3 and cb >= 3 * ca:
            return "B", (f"both medians are over {FAR_M:.0f} m (most incidents in these tiles are on roads "
                         f"outside the configured list), but the within-50 m counts decide for B: {detail}")
        return "neither", (f"neither hypothesis is clearly better: both medians are over {FAR_M:.0f} m and the "
                           f"within-50 m counts do not separate them ({detail}). Either the tiles held few "
                           f"incidents on the M25/M3/M4/A316 this time, or the tile origin is something else "
                           f"again. Run again at a busier time and look at analysis.json.")
    return "unclear", f"the two readings are too close to call: {detail}"


def diff_ids(previous: Sequence[str], current: Sequence[str]) -> dict[str, Any]:
    p, c = set(previous), set(current)
    return {
        "persisted": sorted(p & c),
        "vanished": sorted(p - c),
        "new": sorted(c - p),
    }


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"samples": []}
    samples = data.get("samples") if isinstance(data, dict) else None
    return {"samples": [s for s in (samples or []) if isinstance(s, dict)]}


def save_state(path: Path, state: dict[str, Any]) -> None:
    state = {"samples": state.get("samples", [])[-MAX_STATE_SAMPLES:]}
    write_json(path, state)


def pick_fixture_tile(tiles: Sequence["TileResult"]) -> Optional["TileResult"]:
    """The tile worth keeping as a parser fixture: most breakdown features,
    then most features, among the tiles that came back 200."""
    ok = [t for t in tiles if t.status == 200 and t.content]
    if not ok:
        return None
    return max(ok, key=lambda t: (sum(1 for f in t.features if tt.is_breakdown(f)), len(t.features)))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

@dataclass(slots=True)
class TileResult:
    zoom: int
    x: int
    y: int
    status: Optional[int] = None
    etag: Optional[str] = None
    bytes: int = 0
    elapsed_s: Optional[float] = None
    error: Optional[str] = None
    decode_error: Optional[str] = None
    content: bytes = b""
    features: list = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        return {
            "zoom": self.zoom, "x": self.x, "y": self.y, "status": self.status, "etag": self.etag,
            "bytes": self.bytes, "elapsed_s": round(self.elapsed_s, 3) if self.elapsed_s is not None else None,
            "error": self.error, "decode_error": self.decode_error, "features": len(self.features),
        }


def details_url(key: str, **params: str) -> str:
    """Incident Details URL with the key. Log it only through redacted_url()."""
    if not key:
        raise ValueError("TomTom API key is empty")
    return DETAILS_URL + "?" + urlencode([("key", key), *params.items()])


def details_url_for_log(**params: str) -> str:
    return DETAILS_URL + "?" + urlencode(list(params.items()))


def _snippet(response: Any, n: int = 300) -> str:
    return (response.content or b"")[:n].decode("utf-8", errors="replace").replace("\n", " ").strip()


def _check_quota(response: Any, what: str, outdir: Path, stem: str, took: float) -> None:
    """403 and 429 mean an invalid key or an exhausted month. Save and stop."""
    if response.status_code in (403, 429):
        save_response(outdir, stem, response, body=True, max_body_bytes=QUOTA_BODY_BYTES, elapsed_s=took,
                      note="403/429: invalid key or exhausted quota; probe stopped here")
        interesting = {k: v for k, v in common.redacted_headers(response.headers).items()
                       if k.lower() in ("content-type", "date", "retry-after", "www-authenticate")
                       or "ratelimit" in k.lower() or "quota" in k.lower()}
        raise QuotaStop(what, response.status_code, _snippet(response), interesting)


def fetch_tile(client: Any, key: str, zoom: int, x: int, y: int, *, budget: RequestBudget,
               outdir: Path, etag: Optional[str] = None, stem: Optional[str] = None) -> tuple[TileResult, Optional[str]]:
    """One tile GET. Returns (result, note). Network failures land in
    result.error; 403/429 raise QuotaStop. No retries."""
    res = TileResult(zoom, x, y)
    stem = stem or f"{zoom}_{x}_{y}"
    headers = {"If-None-Match": etag} if etag else None
    budget.count("tiles")
    try:
        response, took = timed_request(client, "GET", tt.tile_url(zoom, x, y, key), headers=headers)
    except common.httpx.HTTPError as exc:
        res.error = explain_exception(exc)
        return res, None
    res.status = response.status_code
    res.elapsed_s = took
    res.bytes = len(response.content or b"")
    res.etag = response.headers.get("etag")
    _check_quota(response, f"tile {zoom}/{x}/{y}", outdir, stem, took)
    save_response(outdir, stem, response, body=True, elapsed_s=took)
    if response.status_code == 200:
        res.content = response.content or b""
        try:
            res.features = tt.decode_tile(res.content, zoom, x, y, y_coord_down=True)
        except Exception as exc:  # a bad tile must not kill the run
            res.decode_error = f"{type(exc).__name__}: {common.scrub(str(exc))}"
    elif response.status_code != 304:
        res.error = f"HTTP {response.status_code} {response.reason_phrase}: {_snippet(response, 160)}"
    return res, None


def fetch_details(client: Any, url: str, *, budget: RequestBudget, outdir: Path, stem: str,
                  what: str) -> tuple[Optional[Any], Optional[dict[str, Any]], Optional[str], Optional[float]]:
    """One Incident Details GET. Returns (response, parsed json, error, seconds)."""
    budget.count("details")
    try:
        response, took = timed_request(client, "GET", url)
    except common.httpx.HTTPError as exc:
        return None, None, explain_exception(exc), None
    _check_quota(response, what, outdir, stem, took)
    save_response(outdir, stem, response, body=True, elapsed_s=took)
    if response.status_code != 200:
        return response, None, f"HTTP {response.status_code} {response.reason_phrase}: {_snippet(response, 200)}", took
    try:
        data = response.json()
    except ValueError:
        return response, None, "the body is not JSON", took
    return response, data, None, took


# --------------------------------------------------------------------------
# One sample
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Sample:
    index: int
    started: datetime
    outdir: Path
    tiles: dict[int, list[TileResult]] = field(default_factory=dict)
    zooms: dict[int, dict[str, Any]] = field(default_factory=dict)
    details: Optional[dict[str, Any]] = None
    details_raw: Any = None
    comparison: dict[int, dict[str, Any]] = field(default_factory=dict)
    by_ids: Optional[dict[str, Any]] = None
    etag_check: Optional[dict[str, Any]] = None
    convention: Optional[dict[str, Any]] = None
    convention_zoom: Optional[int] = None
    failures: list[str] = field(default_factory=list)
    y_setting_agree: Optional[bool] = None
    abandoned: Optional[str] = None                 # why the sample was cut short, or None
    complete_zooms: set[int] = field(default_factory=set)   # every planned tile came back 200 and decoded

    @property
    def breakdown_ids_by_zoom(self) -> dict[str, list[str]]:
        """Breakdown ids per COMPLETE zoom. A zoom with a failed or missing
        tile is left out: its id set could be short, and diffing it against
        another sample would invent 'vanished' incidents."""
        return {str(z): a["breakdown_ids"] for z, a in self.zooms.items() if z in self.complete_zooms}

    @property
    def incomplete_zooms(self) -> list[int]:
        return sorted(z for z in self.zooms if z not in self.complete_zooms)

    @property
    def details_ids(self) -> Optional[list[str]]:
        """Ids from the Details-by-bbox answer, or None when there was no
        usable answer (request failed, 429, --details-limit 0, sample cut
        short). None is not the same as an empty list: an empty list means
        Details really listed nothing."""
        if self.details and self.details.get("ok"):
            return sorted(self.details["ids"])
        return None


def run_sample(sample: Sample, *, client: Any, key: str, plan: dict[str, Any], cfg: dict[str, Any],
               args: Any, budget: RequestBudget, findings: Findings, road_index: Optional[RoadIndex]) -> None:
    box = cfg["box_obj"]
    roads = cfg["roads"]
    outdir = sample.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    findings.kv("Sample folder", common.display_path(outdir))

    # 2. Tiles ---------------------------------------------------------------
    findings.section(f"Sample {sample.index}: tiles")
    consecutive_failures = 0
    for zoom, tile_list in plan["tiles"].items():
        results: list[TileResult] = []
        z0, x0, y0 = tile_list[0]
        findings.add(f"Zoom {zoom}: {len(tile_list)} tile(s). GET {tt.tile_url_for_log(z0, x0, y0)} (key omitted; "
                     f"the other tiles differ only in /{zoom}/x/y.pbf)")
        for (z, x, y) in tile_list:
            res, _ = fetch_tile(client, key, z, x, y, budget=budget, outdir=outdir)
            results.append(res)
            # A transport failure or a 5xx answer both mean "TomTom is not
            # answering": three in a row and the sample stops, so an outage
            # does not burn the whole plan for nothing.
            if res.error and (res.status is None or res.status >= 500):
                consecutive_failures += 1
                findings.add(f"  z{z} {x}/{y}: FAILED: {res.error}")
                sample.failures.append(f"tile {z}/{x}/{y}: {res.error}")
                if consecutive_failures >= CONSECUTIVE_FAILURES_TO_STOP:
                    sample.abandoned = (f"{consecutive_failures} tile requests failed in a row "
                                        f"(the last at zoom {z}, tile {x}/{y})")
                    findings.add(f"  {consecutive_failures} requests failed in a row; giving up on this sample "
                                 f"(no retries). Is the internet up, or is TomTom down? The Details and ETag "
                                 f"steps are skipped; the tiles that did answer are still analysed below.")
                    break
                continue
            consecutive_failures = 0
            line = (f"  z{z} {x}/{y}: HTTP {res.status}, {res.bytes} bytes, "
                    f"{res.elapsed_s:.2f} s, {len(res.features)} feature(s)"
                    + (f", ETag {res.etag}" if res.etag else ", no ETag"))
            if res.decode_error:
                line += f", DECODE FAILED: {res.decode_error}"
                sample.failures.append(f"tile {z}/{x}/{y} decode: {res.decode_error}")
            if res.error:
                line += f", {res.error}"
                sample.failures.append(f"tile {z}/{x}/{y}: {res.error}")
            findings.add(line)
        sample.tiles[zoom] = results
        if sample.abandoned:
            break

    # A zoom counts as complete only when every planned tile came back 200
    # and decoded; otherwise its id set may be short.
    sample.complete_zooms = {
        z for z, results in sample.tiles.items()
        if len(results) == len(plan["tiles"][z])
        and all(r.status == 200 and not r.decode_error for r in results)
    }
    incomplete = [z for z in sample.tiles if z not in sample.complete_zooms]
    if incomplete:
        findings.add(f"Zoom(s) with a failed, undecodable or missing tile: {incomplete}. Their breakdown id "
                     f"sets may be short, so they are left out of the id-stability and zoom-choice tallies "
                     f"(the per-sample tables below still show what came back).")
    missing_zooms = [z for z in plan["tiles"] if z not in sample.tiles]
    if missing_zooms:
        findings.add(f"Zoom(s) not fetched at all (sample cut short): {missing_zooms}")

    write_json(outdir / "tiles.json", [r.record() for zs in sample.tiles.values() for r in zs])

    # 3. Decode and analyse per zoom --------------------------------------
    findings.section(f"Sample {sample.index}: what the tiles hold")
    rows = []
    for zoom, results in sample.tiles.items():
        feats = [f for r in results for f in r.features]
        a = analyse_zoom(zoom, feats)
        sample.zooms[zoom] = a
        ok_tiles = sum(1 for r in results if r.status == 200)
        rows.append([zoom, len(results), ok_tiles, sum(r.bytes for r in results), a["features_raw"],
                     a["edge_duplicates"], a["poi"], a["flow"], a["clusters"], a["breakdown_features"],
                     len(a["breakdown_ids"])])
    findings.table(rows, header=["zoom", "tiles", "HTTP 200", "bytes", "features", "edge dups", "POI (unique)",
                                 "flow (unique)", "clusters", "breakdown POIs", "breakdown ids"])
    for zoom, a in sample.zooms.items():
        findings.add(f"Zoom {zoom}: layers {a['layer_names'] or '(none)'}; category histogram (unique POIs, "
                     f"not clusters): {a['category_histogram'] or '(empty)'}")
        if a["clusters"]:
            sizes = ", ".join(f"{r['category_name']} x{r['size']}" for r in a["cluster_rows"])
            findings.add(f"Zoom {zoom}: {a['clusters']} cluster(s) holding {a['cluster_members_total']} incidents "
                         f"({sizes}); up to {a['cluster_members_that_could_be_breakdowns']} of those could be "
                         f"breakdowns (clusters marked 14 or 13 = mixed) and carry no id")
        if a["breakdowns_without_id"]:
            findings.add(f"Zoom {zoom}: {a['breakdowns_without_id']} breakdown POI(s) without an id tag")
        if a["breakdown_rows"]:
            findings.add(f"Zoom {zoom}: breakdowns (category 14, not clustered):")
            findings.table([[r["id"], r["description"], r["number_of_reports"], r["last_report_time"],
                             r["probability"], r["lat"], r["lon"], f"{r['tile'][1]}/{r['tile'][2]}"]
                            for r in a["breakdown_rows"]],
                           header=["id", "description", "reports", "last_report_time", "probability", "lat", "lon", "tile"])
        else:
            findings.add(f"Zoom {zoom}: no breakdown POIs in these tiles right now")
    # The two library settings must agree once normalised (docstring of tomtom_tiles).
    for results in sample.tiles.values():
        probe_tile = next((r for r in results if r.status == 200 and r.features), None)
        if probe_tile is not None:
            try:
                alt = tt.decode_tile(probe_tile.content, probe_tile.zoom, probe_tile.x, probe_tile.y, y_coord_down=False)
                same = [(f.layer, f.incident_id, f.pixel_coords[:1]) for f in probe_tile.features] == \
                       [(f.layer, f.incident_id, f.pixel_coords[:1]) for f in alt]
            except Exception:
                same = False
            sample.y_setting_agree = same
            findings.add(f"decode_tile with y_coord_down=True and False give the same stored pixels on tile "
                         f"{probe_tile.zoom}/{probe_tile.x}/{probe_tile.y}: {'yes' if same else 'NO (look at the decoder)'}")
            break

    # 4. Incident Details by bbox ------------------------------------------
    if args.details_limit and sample.abandoned:
        findings.section(f"Sample {sample.index}: Incident Details")
        findings.add(f"Skipped: the sample was cut short ({sample.abandoned}); no Details request was sent.")
    elif args.details_limit:
        findings.section(f"Sample {sample.index}: Incident Details by bbox")
        params = dict(bbox=box.bbox_param, fields=FIELDS, language=LANGUAGE, categoryFilter="14",
                      timeValidityFilter="present")
        findings.add(f"GET {details_url_for_log(**params)} (key omitted)")
        response, data, err, took = fetch_details(client, details_url(key, **params), budget=budget, outdir=outdir,
                                                  stem="details_bbox", what="Incident Details by bbox")
        if response is not None:
            findings.add(f"  {status_line(response, took)}")
        if err:
            findings.add(f"  FAILED: {err}")
            sample.failures.append(f"details bbox: {err}")
        else:
            sample.details_raw = data
            d = details_rows(data, box, roads)
            sample.details = d
            if not d["ok"]:
                findings.add(f"  FAILED: {d['error']}")
                sample.failures.append(f"details bbox: {d['error']}")
            else:
                findings.kv(f"Sample {sample.index}: Details incidents (category 14, present)", len(d["ids"]))
                findings.kv(f"Sample {sample.index}: Details incidents on the configured roads", d["on_roads"])
                findings.kv(f"Sample {sample.index}: Details incidents inside the box", d["in_box"])
                findings.table([[r["id"], r["iconCategory"], r["roadNumbers"], r["from"], r["to"], r["startTime"],
                                 r["numberOfReports"], r["geometry"],
                                 f"{r['first'][1]:.5f},{r['first'][0]:.5f}" if r["first"] else None]
                                for r in d["rows"] if r is not None],
                               header=["id", "iconCategory", "roadNumbers", "from", "to", "startTime", "reports",
                                       "geometry", "first lat,lon"])
                details_ids = set(d["ids"])
                for zoom, a in sample.zooms.items():
                    tile_ids = set(a["breakdown_ids"])
                    cmp = compare_ids(details_ids, tile_ids)
                    # Tiles cover more than the box: sort the extras by where they are.
                    positions = {r["id"]: (r["lat"], r["lon"]) for r in a["breakdown_rows"] if r["id"]}
                    extra_in_box = [i for i in cmp["extra_in_tiles"]
                                    if i in positions and positions[i][0] is not None
                                    and box.contains(positions[i][0], positions[i][1])]
                    cmp["extra_in_tiles_inside_box"] = extra_in_box
                    cmp["extra_in_tiles_outside_box"] = [i for i in cmp["extra_in_tiles"] if i not in extra_in_box]
                    sample.comparison[zoom] = cmp
                    findings.add(f"zoom {zoom} misses {len(cmp['missing_from_tiles'])} of {cmp['details']} details ids"
                                 + (f": {', '.join(cmp['missing_from_tiles'])}" if cmp["missing_from_tiles"] else ""))
                    findings.add(f"zoom {zoom} has {len(cmp['extra_in_tiles'])} breakdown id(s) that Details did not list: "
                                 f"{len(extra_in_box)} inside the box"
                                 + (f" ({', '.join(extra_in_box)})" if extra_in_box else "")
                                 + f", {len(cmp['extra_in_tiles_outside_box'])} outside it (tiles cover more than the box)")

        # 5. Details by ids ------------------------------------------------
        findings.section(f"Sample {sample.index}: Incident Details by ids")
        lowest = min(sample.zooms) if sample.zooms else None
        ids = sample.zooms[lowest]["breakdown_ids"][:MAX_IDS_PER_GET] if lowest is not None else []
        if not ids:
            findings.add("Skipped: the lowest zoom's tiles held no breakdown ids to look up (no request sent).")
        else:
            params = dict(ids=",".join(ids), fields=FIELDS, language=LANGUAGE)
            findings.add(f"GET {details_url_for_log(**params)} (key omitted)")
            response, data, err, took = fetch_details(client, details_url(key, **params), budget=budget, outdir=outdir,
                                                      stem="details_by_ids", what="Incident Details by ids")
            if response is not None:
                findings.add(f"  {status_line(response, took)}")
            if err:
                findings.add(f"  FAILED: {err}")
                sample.failures.append(f"details by ids: {err}")
            else:
                d = details_rows(data, box, roads)
                returned = [r["id"] for r in d["rows"] if r is not None and r["id"]]
                matched = [i for i in ids if i in returned]
                sample.by_ids = {"requested": ids, "returned": returned, "nulls": d["nulls"], "matched": matched,
                                 "same_id_space": bool(matched)}
                findings.kv(f"Sample {sample.index}: ids requested from zoom {lowest}", ids)
                findings.kv(f"Sample {sample.index}: ids returned non-null", returned)
                findings.kv(f"Sample {sample.index}: ids returned null (no longer exist)", d["nulls"])
                findings.add("Tile ids and Details ids are the same id space: "
                             + ("yes" if matched else "NOT CONFIRMED (none of the requested ids came back)"))

    # 6. ETag re-request ------------------------------------------------------
    if not args.no_etag_check and sample.abandoned:
        findings.section(f"Sample {sample.index}: ETag check")
        findings.add(f"Skipped: the sample was cut short ({sample.abandoned}); no request sent.")
    elif not args.no_etag_check:
        findings.section(f"Sample {sample.index}: ETag check")
        lowest = min(sample.tiles) if sample.tiles else None
        first = next((r for r in sample.tiles.get(lowest, []) if r.status == 200), None) if lowest is not None else None
        if first is None:
            findings.add("Skipped: no tile came back 200 (no request sent).")
        elif not first.etag:
            findings.add(f"Skipped: tile {first.zoom}/{first.x}/{first.y} carried no ETag header, so there is "
                         f"nothing to send in If-None-Match (no request sent).")
            sample.etag_check = {"etag": None, "status": None}
        else:
            res, _ = fetch_tile(client, key, first.zoom, first.x, first.y, budget=budget, outdir=outdir,
                                etag=first.etag, stem=f"etag_recheck_{first.zoom}_{first.x}_{first.y}")
            sample.etag_check = {"etag": first.etag, "status": res.status, "bytes": res.bytes, "error": res.error}
            if res.error and res.status is None:
                findings.add(f"Re-request of {first.zoom}/{first.x}/{first.y} with If-None-Match FAILED: {res.error}")
                sample.failures.append(f"etag recheck: {res.error}")
            else:
                verdict = ("as expected: an unchanged tile costs no bandwidth (it still counts as a request)"
                           if res.status == 304 else
                           f"expected 304 ({res.bytes} bytes came back): either the tile changed in the last "
                           f"seconds or TomTom ignores If-None-Match")
                findings.add(f"Re-request of {first.zoom}/{first.x}/{first.y} with If-None-Match {first.etag}: "
                             f"HTTP {res.status}, {verdict}")

    # 7. Y-axis convention ------------------------------------------------------
    findings.section(f"Sample {sample.index}: y-axis convention")
    if road_index is None:
        findings.add("Skipped: data/roads.json is missing. Run scripts/fetch_junctions.py and try again.")
    elif not sample.zooms:
        findings.add("Skipped: no tiles were decoded.")
    else:
        top = max(sample.zooms)
        sample.convention_zoom = top
        rows = convention_rows(sample.zooms[top]["unique_features"], road_index, roads)
        s = summarise_convention(rows)
        verdict, reason = judge_convention(s)
        s["verdict"] = verdict
        s["reason"] = reason
        s["rows"] = rows
        sample.convention = s
        findings.kv(f"Sample {sample.index}: zoom tested", top)
        findings.kv(f"Sample {sample.index}: features measured (clusters skipped)", s["n_features"])
        findings.kv(f"Sample {sample.index}: features where A and B differ by >= 100 m", s["n_informative"])
        for name, st in (("A (top-left origin, as decoded)", s["A"]), ("B (mirrored)", s["B"])):
            med = f"{st['median_m']:.0f} m" if st["median_m"] is not None else "n/a"
            findings.add(f"- {name}: median {med}, within 50 m: {st['within_50_m']}, within 200 m: "
                         f"{st['within_200_m']} (of {st['n']}; distances capped at {ROAD_SEARCH_M:.0f} m)")
        findings.kv(f"Sample {sample.index}: y-axis verdict", verdict)
        findings.add(reason)

    write_json(outdir / "analysis.json", {
        "sample": sample.index,
        "started_utc": common.iso(sample.started),
        "started_london": london(sample.started),
        "zooms": {str(z): {k: v for k, v in a.items() if k != "unique_features"} for z, a in sample.zooms.items()},
        "details": sample.details,
        "comparison": {str(z): c for z, c in sample.comparison.items()},
        "details_by_ids": sample.by_ids,
        "etag_check": sample.etag_check,
        "convention_zoom": sample.convention_zoom,
        "convention": sample.convention,
        "failures": sample.failures,
        "abandoned": sample.abandoned,
        "complete_zooms": sorted(sample.complete_zooms),
        "incomplete_zooms": sample.incomplete_zooms,
        "details_ids": sample.details_ids,
    })


def keep_sample_fixtures(sample: Sample, findings: Findings) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for zoom, results in sample.tiles.items():
        best = pick_fixture_tile(results)
        if best is None:
            continue
        dest = FIXTURE_DIR / f"sample_z{zoom}_{best.x}_{best.y}.pbf"
        dest.write_bytes(best.content)
        written.append(common.display_path(dest))
    if sample.details_raw is not None:
        dest = FIXTURE_DIR / "sample_details_bbox.json"
        write_json(dest, sample.details_raw)
        written.append(common.display_path(dest))
    if written:
        findings.add(f"--keep-sample: wrote {', '.join(written)} (parser fixtures; commit them)")
    else:
        findings.add("--keep-sample: nothing to keep (no tile came back 200)")


# --------------------------------------------------------------------------
# Run-level reporting
# --------------------------------------------------------------------------

def print_plan(findings: Findings, plan: dict[str, Any], box: Any) -> None:
    findings.section("Plan")
    for zoom, tiles in plan["tiles"].items():
        findings.add(f"- zoom {zoom}: {len(tiles)} tile(s): " + " ".join(f"{x}/{y}" for _, x, y in tiles))
        w, s, e, n = geo.tile_bounds(zoom, tiles[0][1], tiles[0][2])
        w2, s2, e2, n2 = geo.tile_bounds(zoom, tiles[-1][1], tiles[-1][2])
        findings.add(f"  tiles cover lon {w:.4f}..{e2:.4f}, lat {s2:.4f}..{n:.4f} (box: lon {box.west}..{box.east}, "
                     f"lat {box.south}..{box.north})")
    ps = plan["per_sample"]
    findings.kv("Requests per sample", f"{ps['tiles']} tiles + {ps['details_bbox']} Details by bbox + up to "
                                       f"{ps['details_by_ids']} Details by ids + {ps['etag_recheck']} ETag re-request "
                                       f"= {plan['per_sample_total']}")
    findings.kv("Samples", plan["repeat"])
    findings.kv("Planned requests for this run", plan["total"])
    findings.kv("Planned tile requests (free tier 200,000/month)", plan["tile_requests_total"])
    findings.kv("Planned Details requests (free tier 2,500/month)", plan["details_requests_total"])


def report_stability(findings: Findings, state_samples: Sequence[dict[str, Any]], run_samples: Sequence[Sample],
                     zooms: Sequence[int]) -> dict[str, Any]:
    """Consecutive-pair id diffs over this run's samples, plus the last
    sample of an earlier run when state.json had one.

    A source (a zoom, or Details) is only diffed when BOTH samples of a pair
    have a complete answer from it; otherwise a missing answer would read as
    'N vanished' or 'N appeared'. The totals count DISTINCT ids per pair: the
    union of every comparable zoom (and Details, when comparable) on each
    side, diffed once, so an incident seen at three zooms counts once."""
    findings.section("Id stability across samples")
    entries: list[dict[str, Any]] = []
    n_this_run = len(run_samples)
    earlier = state_samples[:-n_this_run] if n_this_run else list(state_samples)
    if earlier:
        e = earlier[-1]
        raw_ids = e.get("breakdown_ids_by_zoom")
        raw_ids = raw_ids if isinstance(raw_ids, dict) else {}
        raw_details = e.get("details_ids")
        entries.append({"label": f"earlier run {e.get('started_utc')}",
                        "ids": {str(k): [str(i) for i in (v or [])] for k, v in raw_ids.items()},
                        "details": [str(i) for i in raw_details] if isinstance(raw_details, list) else None,
                        "started_utc": e.get("started_utc"), "started_london": e.get("started_london"),
                        "from_earlier_run": True})
    for s in run_samples:
        entries.append({"label": f"sample {s.index}", "ids": s.breakdown_ids_by_zoom, "details": s.details_ids,
                        "started_utc": common.iso(s.started), "started_london": london(s.started),
                        "from_earlier_run": False})
    findings.table([[e["label"], e["started_utc"], e["started_london"],
                     ", ".join(f"z{z}={len(e['ids'][str(z)])}" if str(z) in e["ids"] else f"z{z}=incomplete"
                               for z in zooms),
                     len(e["details"]) if e["details"] is not None else "no answer"]
                    for e in entries],
                   header=["sample", "UTC", "London", "breakdown ids in tiles", "Details ids"])
    pairs: list[dict[str, Any]] = []       # one row per (pair, source), for the table
    distinct: list[dict[str, Any]] = []    # one row per pair: the diff that the totals use
    skipped: list[str] = []
    for prev, cur in zip(entries, entries[1:]):
        label = f"{prev['label']} -> {cur['label']}"
        both_zooms = [z for z in zooms if str(z) in prev["ids"] and str(z) in cur["ids"]]
        for z in zooms:
            if z in both_zooms:
                pairs.append({"pair": label, "source": f"zoom {z}",
                              **diff_ids(prev["ids"][str(z)], cur["ids"][str(z)])})
            else:
                skipped.append(f"{label}: zoom {z} (tiles incomplete or not fetched in one of the two samples)")
        all_prev: set[str] = set()
        all_cur: set[str] = set()
        sources: list[str] = []
        if both_zooms:
            tiles_prev = {i for z in both_zooms for i in prev["ids"][str(z)]}
            tiles_cur = {i for z in both_zooms for i in cur["ids"][str(z)]}
            if len(both_zooms) > 1:
                pairs.append({"pair": label, "source": "tiles, any zoom", **diff_ids(tiles_prev, tiles_cur)})
            all_prev |= tiles_prev
            all_cur |= tiles_cur
            sources.append("tiles")
        if prev["details"] is not None and cur["details"] is not None:
            pairs.append({"pair": label, "source": "Details", **diff_ids(prev["details"], cur["details"])})
            all_prev |= set(prev["details"])
            all_cur |= set(cur["details"])
            sources.append("Details")
        else:
            skipped.append(f"{label}: Details (no usable Details answer in one of the two samples)")
        if sources:
            d = diff_ids(all_prev, all_cur)
            pairs.append({"pair": label, "source": "distinct ids (" + " + ".join(sources) + ")", **d})
            distinct.append({"pair": label, "sources": sources, **d})
        else:
            skipped.append(f"{label}: nothing comparable on both sides, pair not counted")
    if pairs:
        findings.table([[p["pair"], p["source"], len(p["persisted"]), len(p["vanished"]), len(p["new"])]
                        for p in pairs], header=["pair", "ids from", "persisted", "vanished", "new"])
        findings.add("The 'distinct ids' rows count each id once per pair, whichever zooms or Details listed it; "
                     "the totals in (c) sum those rows.")
    elif len(entries) < 2:
        findings.add("Only one sample and no earlier run in state.json: nothing to compare. "
                     "Run with --repeat 2 --interval 120 (or run again later) to test id persistence.")
    else:
        findings.add("Nothing comparable: no pair of consecutive samples had a complete tile set or a Details "
                     "answer on both sides.")
    for line in skipped:
        findings.add(f"Skipped: {line}")
    persisted_total = sum(len(p["persisted"]) for p in distinct)
    vanished_total = sum(len(p["vanished"]) for p in distinct)
    new_total = sum(len(p["new"]) for p in distinct)
    return {"entries": entries, "pairs": pairs, "distinct": distinct, "skipped": skipped,
            "persisted_total": persisted_total, "vanished_total": vanished_total, "new_total": new_total}


def answer_questions(findings: Findings, run_samples: Sequence[Sample], zooms: Sequence[int], plan: dict[str, Any],
                     budget: RequestBudget, stability: dict[str, Any], quota: Optional[QuotaStop],
                     poll_s: float, budget_tiles: int, cfg_zoom: Optional[int], details_enabled: bool) -> None:
    findings.section("Answers")

    # (a) which zoom never missed a Details breakdown id
    findings.add("### (a) Which zoom never missed a breakdown that Incident Details listed")
    never_missed: list[int] = []
    tested = [s for s in run_samples if s.comparison]
    if not details_enabled:
        findings.add("Not tested: --details-limit 0 skipped Incident Details.")
    elif not tested:
        findings.add("Not tested: no sample had a usable Incident Details answer.")
    else:
        total_details = sum(next(iter(s.comparison.values()))["details"] for s in tested)
        rows = []
        checked_by_zoom: dict[int, int] = {}
        for z in zooms:
            # Only a complete zoom (every tile answered) can be blamed for a miss.
            usable = [s for s in tested if z in s.comparison and z in s.complete_zooms]
            checked = sum(s.comparison[z]["details"] for s in usable)
            missed = sum(len(s.comparison[z]["missing_from_tiles"]) for s in usable)
            names = sorted({i for s in usable for i in s.comparison[z]["missing_from_tiles"]})
            checked_by_zoom[z] = checked
            rows.append([z, plan["tiles_per_zoom"][z], len(usable), checked, missed, ", ".join(names) or "(none)"])
            # A zoom is proven only when it was actually tested against at
            # least one Details id: zero ids checked proves nothing.
            if missed == 0 and checked > 0:
                never_missed.append(z)
        findings.table(rows, header=["zoom", "tiles", "samples compared", "Details ids checked",
                                     "Details ids missed", "missed ids"])
        if total_details == 0:
            findings.add(f"Details listed no breakdowns in {len(tested)} sample(s), so there was nothing to miss "
                         f"and NO zoom is proven by this run. Run again at a busier time before choosing a zoom.")
        elif never_missed:
            findings.add(f"Zoom(s) that never missed a Details id in this run: {never_missed}. The lowest of "
                         f"these, zoom {min(never_missed)}, is the cheapest choice supported by this run.")
        elif any(checked_by_zoom.values()):
            findings.add("Every zoom that could be compared missed at least one Details id in this run. "
                         "Clustering (or a breakdown shown only as a flow line without a POI) hides some "
                         "incidents at every zoom tested; consider a higher zoom or a Details bbox call for the box.")
        else:
            findings.add("Not proven: every sample with a Details answer had a failed or missing tile at every "
                         "zoom, so no zoom could be compared. Run again.")
        findings.kv("(a) zooms that never missed a Details id", never_missed)
        findings.kv("(a) Details ids checked in this run", total_details)
        findings.kv("(a) a zoom was proven by this run", bool(never_missed))

    # (b) y-axis convention
    findings.add("")
    findings.add("### (b) Y-axis convention of the decoded tiles")
    verdicts = [(s.index, s.convention_zoom, s.convention["verdict"], s.convention["reason"])
                for s in run_samples if s.convention]
    if not verdicts:
        findings.add("Not tested: no tiles decoded or data/roads.json missing.")
    else:
        counts = Counter(v for _, _, v, _ in verdicts)
        for i, z, v, reason in verdicts:
            findings.add(f"- sample {i} (zoom {z}): {v}: {reason}")
        if counts.get("A") and not counts.get("B"):
            findings.add("Verdict: hypothesis A. The stored py is measured from the tile's TOP edge (MVT spec), "
                         "so decode_tile(raw_flip=False), as implemented, is right; nothing to change.")
        elif counts.get("B") and not counts.get("A"):
            findings.add("Verdict: hypothesis B. The stored py is measured from the tile's BOTTOM edge: the "
                         "service must decode with raw_flip=True (mirror_coords).")
        elif counts.get("A") and counts.get("B"):
            findings.add("Verdict: CONTRADICTORY between samples; do not trust either yet. Send analysis.json.")
        elif counts.get("untested") == len(verdicts):
            findings.add("Not tested: no incidents were decoded at the tested zoom (or none sat far enough from "
                         "the middle of their tile to tell A from B), so nothing was measured. Run again at a "
                         "busier time (weekday rush hour) so the tiles hold incidents on the M25/M4.")
        else:
            findings.add("Verdict: neither hypothesis was clearly better in the sample(s) that had incidents. "
                         "Run again at a busier time (weekday rush hour) so the tiles hold incidents on the "
                         "M25/M4, and look at analysis.json.")
        findings.kv("(b) y-axis verdicts by sample", {str(i): v for i, _, v, _ in verdicts})

    # (c) id persistence
    findings.add("")
    findings.add("### (c) Did ids persist between samples")
    n_pairs = len(stability["distinct"])
    if len(stability["entries"]) < 2:
        findings.add("Not tested: one sample only. Run with --repeat 2 --interval 120 or run again later "
                     "(state.json remembers the ids).")
    elif not n_pairs:
        findings.add("Not tested: no pair of consecutive samples had a complete tile set or a Details answer "
                     "on both sides (see the skipped list above), so nothing could be compared.")
    else:
        p, v, n = stability["persisted_total"], stability["vanished_total"], stability["new_total"]
        if p:
            findings.add(f"Yes: across {n_pairs} pair(s) of consecutive samples, {p} distinct id(s) persisted, "
                         f"{v} vanished (the incident cleared or the sample gap was too long) and {n} appeared; "
                         f"each id is counted once per pair, whichever zooms or Details listed it. An incident "
                         f"keeps its id while it is live, so the service can key its cases on it.")
        elif v or n:
            findings.add(f"No id persisted: across {n_pairs} pair(s), {v} distinct id(s) vanished and {n} appeared "
                         f"with none in common. Either every incident cleared between samples (long gap, quiet "
                         f"roads) or ids change from poll to poll. Repeat with --interval 120 to tell those apart.")
        else:
            findings.add(f"No breakdown ids in any of the {n_pairs} compared pair(s), so persistence could not "
                         f"be judged.")
    findings.kv("(c) pairs compared", n_pairs)
    findings.kv("(c) distinct ids persisted / vanished / new", f"{stability['persisted_total']} / "
                                                              f"{stability['vanished_total']} / {stability['new_total']}")

    # (d) 403 / 429
    findings.add("")
    findings.add("### (d) What a 403/429 looked like")
    if quota is None:
        findings.add("Not observed: every answer was 200 or 304.")
        findings.kv("(d) 403/429 observed", "not observed")
    else:
        findings.add(f"HTTP {quota.status} on {quota.what}. Body starts: {quota.snippet!r}. "
                     f"Headers of note: {quota.headers or '(none)'}. This means an invalid key or the month's "
                     f"quota is used up; the service must stop this product until next month, not retry.")
        findings.kv("(d) 403/429 observed", f"HTTP {quota.status} on {quota.what}")

    # (e) request counts and monthly projection
    findings.add("")
    findings.add("### (e) Request counts and projected monthly cost")
    findings.kv("Requests sent this run", budget.totals())
    polls = polls_per_month(poll_s)
    findings.add(f"Polling every {poll_s:.0f} s is {polls:.0f} polls in a 31-day month "
                 f"(31 x 24 x 60 / {poll_s / 60:.0f}). Every request counts, 304 answers included.")
    rows = []
    for r in monthly_projection(plan["tiles_per_zoom"], poll_s, budget_tiles, FREE_TILES_PER_MONTH):
        rows.append([r["zoom"], r["tiles_per_poll"], f"{r['requests_per_month']:,}",
                     f"{r['share_of_free_tier'] * 100:.1f}%", "yes" if r["fits_free_tier"] else "NO",
                     f"every {r['fastest_poll_allowed_by_budget_s'] / 60:.1f} min or slower"])
    findings.table(rows, header=["zoom", "tiles/poll", "tile requests/month", "of free 200,000", "fits",
                                 f"poll allowed by budget {budget_tiles:,}"])
    chosen = min(never_missed) if never_missed else cfg_zoom
    if chosen is not None and chosen in plan["tiles_per_zoom"]:
        n = plan["tiles_per_zoom"][chosen]
        per_month = n * polls
        why = ("the lowest zoom that never missed a Details id" if never_missed
               else "from config poll.tomtom_zoom, because no zoom was proven by this run")
        findings.add(f"Chosen zoom {chosen} ({why}): {n} tiles x {polls:.0f} polls = {per_month:,.0f} tile requests "
                     f"a month, {per_month / FREE_TILES_PER_MONTH * 100:.1f}% of the free tier"
                     + ("." if per_month <= budget_tiles else
                        f"; OVER the configured budget of {budget_tiles:,}, so poll no faster than every "
                        f"{n * DAYS_PER_MONTH * 24 * 3600 / budget_tiles / 60:.1f} min at this zoom."))
        findings.kv("(e) chosen zoom", chosen)
        findings.kv("(e) projected tile requests per month at chosen zoom", round(per_month))
    findings.add(f"Incident Details: the free tier is {FREE_DETAILS_PER_MONTH:,} a month, about "
                 f"{FREE_DETAILS_PER_MONTH / DAYS_PER_MONTH:.0f} a day, so the service cannot poll Details; it can "
                 f"only look up new breakdown ids (up to 5 per GET) for road numbers and place names.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    cfg, outdir, parser = common.setup(
        "tomtom",
        "Fetch TomTom incident tiles at several zooms plus one Incident Details call for the config box, "
        "compare them, check the tile y-axis convention against OpenStreetMap road geometry, and (with "
        "--repeat) whether incident ids persist. Prints the request plan first and never exceeds --max-requests.",
        argv=argv, max_requests_default=DEFAULT_MAX_REQUESTS,
    )
    parser.add_argument("--zooms", default=DEFAULT_ZOOMS, metavar="Z,Z,...",
                        help=f"tile zoom levels to fetch (default: {DEFAULT_ZOOMS})")
    parser.add_argument("--plan-only", action="store_true",
                        help="print the tile list and request plan, send nothing, exit 0 (no key needed)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N", help="number of samples (default: 1)")
    parser.add_argument("--interval", type=float, default=0.0, metavar="SECONDS",
                        help="seconds to wait between samples (default: 0)")
    parser.add_argument("--details-limit", type=int, default=1, metavar="N",
                        help="Incident Details bbox calls per sample: 1 (default) or 0 to skip Details entirely")
    parser.add_argument("--keep-sample", action="store_true",
                        help="copy one tile per zoom and the Details body into fixtures/tomtom/ as parser fixtures")
    parser.add_argument("--no-etag-check", action="store_true",
                        help="skip the If-None-Match re-request of the first tile")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), metavar="FILE",
                        help="where breakdown ids per sample are remembered (default: fixtures/tomtom/live/state.json)")
    args = parser.parse_args(argv)

    def usage_error(msg: str) -> int:
        print(msg, file=sys.stderr, flush=True)
        common.remove_if_empty(outdir)
        return EXIT_FAILED

    try:
        zooms = parse_zooms(args.zooms)
    except ValueError as exc:
        return usage_error(str(exc))
    if args.repeat < 1:
        return usage_error("--repeat must be at least 1")
    if args.max_requests is None or args.max_requests < 1:
        # common.positive_int already refuses this at parse time; kept so a
        # bad value can never reach RequestBudget as a traceback.
        return usage_error("--max-requests must be at least 1")
    if args.interval < 0:
        return usage_error("--interval must be 0 or more seconds")
    if args.details_limit not in (0, 1):
        return usage_error("--details-limit must be 0 or 1 (the bbox query is the same every time; 1 is enough)")
    if args.repeat > 1 and args.interval < 1:
        print("Note: --repeat without --interval takes the samples back to back; pass --interval 120 "
              "to space them like the service will.", flush=True)

    box = cfg["box_obj"]
    roads = cfg["roads"]
    poll_s = float((cfg.get("poll") or {}).get("tomtom_tiles") or DEFAULT_POLL_S)
    budget_tiles = int((cfg.get("budget") or {}).get("tomtom_tiles") or FREE_TILES_PER_MONTH)
    cfg_zoom = (cfg.get("poll") or {}).get("tomtom_zoom")
    cfg_zoom = int(cfg_zoom) if isinstance(cfg_zoom, int) else None

    plan = build_plan(box, zooms, args.details_limit, not args.no_etag_check, args.repeat)
    findings = Findings("TomTom probe")
    findings.kv("Run started (UTC)", common.iso(now_utc()))
    findings.kv("Run started (Europe/London)", london(now_utc()))
    findings.kv("Box (W,S,E,N)", f"{box.west}, {box.south}, {box.east}, {box.north}")
    findings.kv("Roads", roads)
    findings.kv("Zooms", zooms)
    print_plan(findings, plan, box)

    budget = RequestBudget(args.max_requests)
    if args.plan_only:
        findings.section("Monthly projection (every request counts)")
        findings.table([[r["zoom"], r["tiles_per_poll"], f"{r['requests_per_month']:,}",
                         f"{r['share_of_free_tier'] * 100:.1f}%", "yes" if r["fits_free_tier"] else "NO",
                         f"every {r['fastest_poll_allowed_by_budget_s'] / 60:.1f} min or slower"]
                        for r in monthly_projection(plan["tiles_per_zoom"], poll_s, budget_tiles)],
                       header=["zoom", "tiles/poll", f"requests/month at every {poll_s:.0f} s", "of free 200,000",
                               "fits", f"poll allowed by budget {budget_tiles:,}"])
        try:
            budget.check(plan["total"], "tiles + Details + ETag re-request")
        except SystemExit as exc:
            print(f"A real run would refuse: {exc}", flush=True)
        print("--plan-only: nothing was sent.", flush=True)
        common.remove_if_empty(outdir)
        return EXIT_OK

    key = secret("TOMTOM_API_KEY")
    if not key:
        missing_key("TOMTOM_API_KEY", outdir)
    budget.check(plan["total"], "tiles + Details + ETag re-request")

    roads_path = Path(str((cfg.get("storage") or {}).get("roads") or "data/roads.json"))
    if not roads_path.is_absolute():
        roads_path = _ROOT / roads_path
    road_index: Optional[RoadIndex] = None
    if roads_path.is_file():
        try:
            road_index = RoadIndex.load(roads_path)
            findings.kv("Road geometry", f"{common.display_path(roads_path)}: {len(road_index)} ways "
                                         f"({road_index.attribution or 'OpenStreetMap'})")
        except Exception as exc:
            findings.add(f"Could not load {common.display_path(roads_path)}: {exc}; the y-axis check will be skipped")
    else:
        findings.add(f"{common.display_path(roads_path)} is missing; the y-axis check will be skipped "
                     f"(run scripts/fetch_junctions.py)")

    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    run_samples: list[Sample] = []
    quota: Optional[QuotaStop] = None
    rc = EXIT_OK
    try:
        with common.http_client() as client:
            for i in range(1, args.repeat + 1):
                if i > 1 and args.interval > 0:
                    wake = now_utc() + timedelta(seconds=args.interval)
                    findings.add(f"Next sample {i} of {args.repeat} at {common.iso(wake)} ({london(wake)} London); "
                                 f"sleeping {args.interval:.0f} s (Ctrl-C to stop)")
                    time.sleep(args.interval)
                sample = Sample(index=i, started=now_utc(), outdir=outdir / f"sample_{i:02d}")
                findings.section(f"Sample {i} of {args.repeat}: {common.iso(sample.started)} "
                                 f"({london(sample.started)} London)")
                run_samples.append(sample)
                try:
                    run_sample(sample, client=client, key=key, plan=plan, cfg=cfg, args=args, budget=budget,
                               findings=findings, road_index=road_index)
                finally:
                    # Remember the ids even when the sample was cut short.
                    state["samples"].append({
                        "run": common.display_path(outdir), "sample": i,
                        "started_utc": common.iso(sample.started), "started_london": london(sample.started),
                        # Complete zooms only; None = no usable Details answer (not "none listed").
                        "breakdown_ids_by_zoom": sample.breakdown_ids_by_zoom,
                        "incomplete_zooms": sample.incomplete_zooms,
                        "details_ids": sample.details_ids,
                        "abandoned": sample.abandoned,
                        "breakdown_count_by_zoom": {z: len(v) for z, v in sample.breakdown_ids_by_zoom.items()},
                    })
                    save_state(state_path, state)
                if args.keep_sample:
                    keep_sample_fixtures(sample, findings)
                if sample.failures:
                    rc = EXIT_FAILED
                    findings.add(f"Sample {i} had {len(sample.failures)} failure(s); see above. No retries were made.")
                if args.repeat > 1:
                    findings.write(outdir)    # keep findings.md current during a long run
    except QuotaStop as exc:
        quota = exc
        findings.add(f"STOP: TomTom answered HTTP {exc.status} on {exc.what}. This looks like an invalid API key "
                     f"or an exhausted monthly quota (TomTom uses 403 and 429 for both). Neither is worth a "
                     f"retry: check the key in .env and the usage on developer.tomtom.com. The body was saved; "
                     f"nothing more was sent.")
        rc = EXIT_QUOTA
    except KeyboardInterrupt:
        findings.add(f"Stopped by Ctrl-C after {len(run_samples)} sample(s); reporting what was collected.")
        rc = EXIT_FAILED

    stability = report_stability(findings, state["samples"], run_samples, zooms)
    answer_questions(findings, run_samples, zooms, plan, budget, stability, quota, poll_s, budget_tiles, cfg_zoom,
                     details_enabled=bool(args.details_limit))
    findings.section("Requests")
    findings.kv("Requests sent", budget.totals())
    findings.kv("Per-run cap", args.max_requests)
    findings.kv("State file", common.display_path(state_path))
    findings.kv("Exit code", rc)
    findings.write(outdir)
    return rc


if __name__ == "__main__":
    sys.exit(main())
