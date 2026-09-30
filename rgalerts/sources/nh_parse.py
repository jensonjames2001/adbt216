"""National Highways "Road and Lane Closures" (DATEX II v3.4 JSON) parser.

Parsing only: nothing in this module touches the network. The probe and the
later service fetch the JSON and hand it to parse_payload().

Endpoint facts (from the developer portal's own API definition, fixtures/nh/):
- GET https://api.data.nationalhighways.co.uk/roads/v2.0/closures
      ?closureType=unplanned&startDateTime=...&endDateTime=...
      [&modifiedSinceDateTime=...][&pageCursor=N]
  Date-times are "YYYY-MM-DDThh:mm:ss" with no timezone suffix; we send UTC.
- Header "Ocp-Apim-Subscription-Key" is required. The default response is XML,
  so "X-Response-MediaType: application/json" is always sent.
- Pagination: the response header "x-next" carries the URL of the next page.
  The first call must be made without pageCursor.

Payload shape: {"D2Payload": {"situation": [{"idG", "situationVersionTime",
"situationRecord": [{<recordTypeKey>: {...record...}}, ...]}, ...]}}.
The portal only documents the record type key
"sitRoadOrCarriagewayOrLaneManagement", but DATEX II has many more
(sitAccident, sitVehicleObstruction, ...), so every key of every
situationRecord entry is treated as a record.

Coordinates: posList is "lat lon lat lon ..." (GML axis order for EPSG:4326;
confirmed by the fixtures, where the first number of each pair is 51..53 and
the second is -2.1..0.5). A magnitude check (UK: lat 49..61, lon -9..3)
guards against the order ever flipping. Points are returned as (lon, lat)
tuples, matching Report.line.

Rules from the build brief honoured here:
- Never invent a road, junction, direction or cause: only the feed's own
  roadName / direction / cause fields are used, never free text.
- Store no personal data: the feed carries none, and we only copy the fields
  listed in to_report().
- Times are tz-aware UTC. "First reported" is the earlier of the validity
  start and the record's creation time, never a scheduled future start.
- A location group whose members disagree on road or direction (NH models
  "both directions" as one member per carriageway) gives None for that field;
  every value the feed carried is kept in Report.extra (road_names,
  directions_raw, lines).
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rgalerts.config import redact
from rgalerts.models import Report

log = logging.getLogger("rgalerts.nh_parse")

# --------------------------------------------------------------------------
# Endpoint constants and request helpers
# --------------------------------------------------------------------------

NH_BASE = "https://api.data.nationalhighways.co.uk/roads/v2.0/closures"
HEADER_KEY = "Ocp-Apim-Subscription-Key"
HEADER_MEDIA = "X-Response-MediaType"
HEADER_FORMAT = "X-Data-Format"
NEXT_PAGE_HEADER = "x-next"
DT_FORMAT = "%Y-%m-%dT%H:%M:%S"  # what the portal calls YYYY-MM-DDThh:mm:ss

# Query parameters that may carry a secret (the portal also accepts the key
# as ?subscription-key=...; an x-next URL might echo it back).
SECRET_QUERY_PARAMS = ("subscription-key", "key", "apikey", "api_key")

# UK bounding ranges used only to sanity-check coordinate order.
UK_LAT = (49.0, 61.0)
UK_LON = (-9.0, 3.0)

# causeType == vehicleObstruction with one of these detailed values is a
# breakdown lead. Everything else on the VehicleObstructionTypeEnum is not:
# abnormalLoad, convoy, slowVehicle, ... are not breakdowns; an
# abandonedVehicle has no driver to phone a recovery firm; a vehicleOnFire is
# a fire-service job first. Those still show up in distinct_values() so the
# probe logs them.
BREAKDOWN_CAUSES = frozenset({
    "brokenDownVehicle",
    "damagedVehicle",
    "vehicleInDifficulty",
    "vehicleStuck",
})

# Lane usages that mean "off the running lanes" (LaneEnum values plus the
# NH extension codes lh/rh = left/right hard shoulder).
SHOULDER_LANE_USAGES = frozenset({
    "hardShoulder", "emergencyLane", "layBy", "verge", "lh", "rh",
})
# Carriageway extension values that are themselves off the running lanes.
SHOULDER_CARRIAGEWAYS = frozenset({"layBy", "emergencyArea"})

# Record type keys other than sitRoadOrCarriagewayOrLaneManagement that
# DATEX II defines. When such a record has no explicit "cause" block, its
# class name IS the cause classification, and the detailed type sits on the
# record itself under the named field.
RECORD_TYPE_CAUSES = {
    "sitAccident": ("accident", "accidentType"),
    "sitVehicleObstruction": ("vehicleObstruction", "vehicleObstructionType"),
    "sitObstruction": ("obstruction", "obstructionType"),
    "sitEnvironmentalObstruction": ("environmentalObstruction", "environmentalObstructionType"),
    "sitInfrastructureDamageObstruction": ("infrastructureDamageObstruction", "infrastructureDamageType"),
    "sitRoadMaintenance": ("roadMaintenance", "roadMaintenanceType"),
}


def headers(key: str) -> dict[str, str]:
    """Request headers for the closures endpoint. The key is never logged."""
    if not key or not str(key).strip():
        raise ValueError("National Highways API key is empty: set NH_API_KEY in .env")
    return {
        HEADER_KEY: str(key).strip(),
        HEADER_MEDIA: "application/json",
        HEADER_FORMAT: "DATEXII",
    }


def fmt_dt(dt: datetime) -> str:
    """Format a tz-aware datetime as UTC "YYYY-MM-DDThh:mm:ss" (no suffix,
    no fraction), which is the only form the portal documents."""
    if dt.tzinfo is None:
        raise ValueError("fmt_dt needs a timezone-aware datetime (use datetime.now(timezone.utc))")
    return dt.astimezone(timezone.utc).strftime(DT_FORMAT)


def build_params(now_utc: datetime, window_hours: float = 6,
                 modified_since_min: Optional[float] = None) -> dict[str, str]:
    """Query parameters for one first-page call: unplanned closures that
    started in the last `window_hours`, optionally only those modified in the
    last `modified_since_min` minutes (portal advice: 6 h / 15 min)."""
    if now_utc.tzinfo is None:
        raise ValueError("build_params needs a timezone-aware datetime (use datetime.now(timezone.utc))")
    now_utc = now_utc.astimezone(timezone.utc)
    params = {
        "closureType": "unplanned",
        "startDateTime": fmt_dt(now_utc - timedelta(hours=float(window_hours))),
        "endDateTime": fmt_dt(now_utc),
    }
    if modified_since_min is not None:
        params["modifiedSinceDateTime"] = fmt_dt(now_utc - timedelta(minutes=float(modified_since_min)))
    return params


def next_page_url(response_headers: Any) -> Optional[str]:
    """The x-next header (case-insensitive), or None when there is no next page.
    Accepts a plain dict, a list of (name, value) pairs or httpx.Headers."""
    if response_headers is None:
        return None
    items = response_headers.items() if hasattr(response_headers, "items") else response_headers
    for name, value in items:
        if str(name).lower() == NEXT_PAGE_HEADER:
            value = str(value).strip() if value is not None else ""
            return value or None
    return None


def redact_url(url: str, *secrets: Optional[str]) -> str:
    """Mask secrets in a URL so it can be printed or saved.

    Two passes: the named secret query parameters (subscription-key, key,
    ...) are masked by name, then every value in `secrets` is masked wherever
    it appears (path, query or fragment) with rgalerts.config.redact.

    Name-based masking alone is NOT sufficient: the portal's own x-next
    example shows a masked segment in the PATH
    ("closures/*************&PageCursor=..."), so a caller that holds the key
    must pass it: redact_url(url, key). None/empty secrets are ignored."""
    result = url
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is not None and parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        cleaned = [(k, "***" if k.lower() in SECRET_QUERY_PARAMS and v else v) for k, v in pairs]
        result = urlunsplit(parts._replace(query=urlencode(cleaned, safe="*:/,")))
    return redact(result, *secrets)


# --------------------------------------------------------------------------
# Small value helpers
# --------------------------------------------------------------------------

def _as_list(v: Any) -> list:
    """The JSON feed uses lists, but an XML-derived variant could give a
    single object where a list is expected. Normalise to a list."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _as_dict(v: Any) -> dict:
    """The value when it is a dict, else {}: a malformed record (a string or
    a list where an object is expected) must never raise."""
    return v if isinstance(v, dict) else {}


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _enum_value(v: Any) -> Optional[str]:
    """DATEX II enums arrive as a plain string or as {"value": X,
    "extendedValueG": Y}. When value is the placeholder "extendedG", the
    real value is in extendedValueG (e.g. lane "cl2", carriageway
    "dualCarriageway")."""
    if v is None:
        return None
    if isinstance(v, dict):
        value = _str(v.get("value"))
        ext = _str(v.get("extendedValueG"))
        if ext and (value is None or value == "extendedG"):
            return ext
        return value
    return _str(v)


def _enum_values(v: Any) -> list[str]:
    """Like _enum_value but for fields that may be a list of enums."""
    out: list[str] = []
    for item in _as_list(v):
        s = _enum_value(item)
        if s:
            out.append(s)
    return out


def parse_iso(s: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string ("2025-03-20T11:03:12Z", fractional seconds,
    offsets, or no suffix = UTC) into a tz-aware UTC datetime. None on failure."""
    s = _str(s)
    if s is None:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        log.warning("NH: unparseable date-time %r", s)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Direction
# --------------------------------------------------------------------------

_NON_LETTERS = re.compile(r"[^a-z]+")


def normalise_direction(s: Any) -> Optional[str]:
    """Map a DATEX II DirectionEnum value (or a typo of one) to one of
    "clockwise", "anticlockwise", "northbound", "southbound", "eastbound",
    "westbound", "both", or None when nothing can be read from it.

    Tolerant of camelCase (northBound), typos (souththbound, the portal's own
    sample) and bothWays. Values that carry no absolute direction (aligned,
    opposite, other, innerRing, outerRing, northEastBound, ...) give None:
    they are never guessed. The enum's own "unknown" also gives None, which
    is the one "unknown" marker models.py defines; the raw value survives in
    extra["direction_raw"]."""
    raw = _str(s)
    if raw is None:
        return None
    t = _NON_LETTERS.sub("", raw.lower())
    if not t:
        return None
    if "clockwise" in t:
        return "anticlockwise" if (t.startswith("anti") or t.startswith("counter")) else "clockwise"
    if t in ("both", "bothways", "bothdirections", "alldirections"):
        return "both"
    if t.endswith("bound"):
        t = t[:-5]
    has_ns = ("north" in t) or ("south" in t)
    has_ew = ("east" in t) or ("west" in t)
    if has_ns and has_ew:
        return None  # northEastBound etc.: not representable, never guessed
    if t.startswith("north"):
        return "northbound"
    if t.startswith("south"):
        return "southbound"
    if t.startswith("east"):
        return "eastbound"
    if t.startswith("west"):
        return "westbound"
    return None


# --------------------------------------------------------------------------
# Coordinates
# --------------------------------------------------------------------------

def _in_range(v: float, rng: tuple[float, float]) -> bool:
    return rng[0] <= v <= rng[1]


def poslist_order(pairs: list[tuple[float, float]]) -> Optional[str]:
    """Detect whether (a, b) pairs are "latlon" or "lonlat" by UK magnitude.
    None when neither order fits every pair (outside the UK or garbage)."""
    if not pairs:
        return None
    if all(_in_range(a, UK_LAT) and _in_range(b, UK_LON) for a, b in pairs):
        return "latlon"
    if all(_in_range(b, UK_LAT) and _in_range(a, UK_LON) for a, b in pairs):
        return "lonlat"
    return None


def parse_poslist(poslist: Any, srs_dimension: Any = 2) -> list[tuple[float, float]]:
    """Parse a GML posList ("lat lon lat lon ...", space separated) into
    [(lon, lat), ...]. srsDimension 3 drops the height. The documented and
    fixture-confirmed order is lat lon; the magnitude check only overrides
    it when every pair clearly reads as lon lat."""
    if poslist is None:
        return []
    if isinstance(poslist, (list, tuple)):
        text = " ".join(str(x) for x in poslist)
    else:
        text = str(poslist)
    tokens = text.replace(",", " ").split()
    if not tokens:
        return []
    try:
        nums = [float(t) for t in tokens]
    except ValueError:
        log.warning("NH: posList has a non-numeric token: %r", text[:80])
        return []
    try:
        dim = int(srs_dimension) if srs_dimension is not None else 2
    except (TypeError, ValueError):
        dim = 2
    if dim < 2:
        dim = 2
    if len(nums) % dim:
        log.warning("NH: posList length %d is not a multiple of srsDimension %d; trailing values dropped", len(nums), dim)
    pairs = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - dim + 1, dim)]
    order = poslist_order(pairs)
    if order == "lonlat":
        log.warning("NH: posList read as lon lat (magnitude check); the documented order is lat lon")
        return [(a, b) for a, b in pairs]
    if order is None:
        log.warning("NH: posList coordinates fall outside UK ranges; assuming lat lon order")
    return [(b, a) for a, b in pairs]


def _point_from_coordinates(pc: Any) -> Optional[tuple[float, float]]:
    """pointByCoordinates.pointCoordinates{latitude, longitude} -> (lon, lat)."""
    if not isinstance(pc, dict):
        return None
    try:
        lat = float(pc["latitude"])
        lon = float(pc["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lon, lat)


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------

@dataclass
class _LocParts:
    """Accumulator used while walking one locationReference."""
    road_names: list[str] = field(default_factory=list)
    directions_raw: list[str] = field(default_factory=list)
    carriageways: list[str] = field(default_factory=list)
    lanes: list[dict] = field(default_factory=list)
    lanes_restricted: Optional[int] = None
    lanes_operational: Optional[int] = None
    descriptions: list[str] = field(default_factory=list)

    def add_road(self, name: Optional[str]) -> None:
        if name and name not in self.road_names:
            self.road_names.append(name)

    def add_direction(self, d: Optional[str]) -> None:
        if d and d not in self.directions_raw:
            self.directions_raw.append(d)


def _walk_linear_element(el: Any, parts: _LocParts) -> None:
    """linearElement{locLinearElementByCode{roadName}} -> road name."""
    if not isinstance(el, dict):
        return
    by_code = el.get("locLinearElementByCode")
    if isinstance(by_code, dict):
        parts.add_road(_str(by_code.get("roadName")))


def _walk_supplementary(spd: Any, parts: _LocParts) -> None:
    """supplementaryPositionalDescription -> description, carriageways, lanes."""
    if not isinstance(spd, dict):
        return
    desc = _str(spd.get("locationDescription"))
    if desc and desc not in parts.descriptions:
        parts.descriptions.append(desc)
    for cw in _as_list(spd.get("carriageway")):
        if not isinstance(cw, dict):
            continue
        cw_value = _enum_value(cw.get("carriageway"))
        if cw_value:
            parts.carriageways.append(cw_value)
        for lane in _as_list(cw.get("lane")):
            if not isinstance(lane, dict):
                continue
            impact = _as_dict(_as_dict(_as_dict(lane.get("laneExtensionG"))
                                       .get("impactOnLanes")).get("impactExtensionG"))
            number = lane.get("laneNumber")
            try:
                number = int(number) if number is not None else None
            except (TypeError, ValueError):
                number = None
            parts.lanes.append({
                "number": number,
                "usage": _enum_value(lane.get("laneUsage")),
                "status": _str(impact.get("lanesStatus")),
                "direction": _str(impact.get("laneImpactDirection")),
                "carriageway": cw_value,
            })
        impact_cw = _as_dict(_as_dict(cw.get("carriagewayExtensionG")).get("impactOnCarriageway"))
        for key, attr in (("numberOfLanesRestricted", "lanes_restricted"),
                          ("numberOfOperationalLanes", "lanes_operational")):
            v = impact_cw.get(key)
            if v is None:
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            cur = getattr(parts, attr)
            # Several carriageways: keep the largest figure (most restrictive).
            setattr(parts, attr, v if cur is None else max(cur, v))


def _walk_single_road(srl: Any, parts: _LocParts) -> None:
    """locSingleRoadLinearLocation{linearWithinLinearElement[]} -> road, direction."""
    if not isinstance(srl, dict):
        return
    for lwle in _as_list(srl.get("linearWithinLinearElement")):
        if not isinstance(lwle, dict):
            continue
        parts.add_direction(_str(lwle.get("directionOnLinearSection")))
        _walk_linear_element(lwle.get("linearElement"), parts)


def _walk_point(pl: Any, parts: _LocParts) -> list[tuple[float, float]]:
    """locPointLocation -> [(lon, lat)] (one point, or empty)."""
    if not isinstance(pl, dict):
        return []
    pt = _point_from_coordinates(_as_dict(pl.get("pointByCoordinates")).get("pointCoordinates"))
    for pale in _as_list(pl.get("pointAlongLinearElement")):
        if not isinstance(pale, dict):
            continue
        parts.add_direction(_str(pale.get("directionAtPoint")))
        _walk_linear_element(pale.get("linearElement"), parts)
    _walk_supplementary(pl.get("supplementaryPositionalDescription"), parts)
    return [pt] if pt else []


def _walk_linear(ll: Any, parts: _LocParts) -> list[tuple[float, float]]:
    """locLinearLocation -> polyline [(lon, lat), ...]."""
    if not isinstance(ll, dict):
        return []
    gml = _as_dict(_as_dict(ll.get("gmlLineString")).get("locGmlLineString"))
    pts = parse_poslist(gml.get("posList"), gml.get("srsDimension", 2))
    _walk_supplementary(ll.get("supplementaryPositionalDescription"), parts)
    return pts


def _walk_location(loc: Any, parts: _LocParts) -> tuple[Optional[str], list[tuple[float, float]]]:
    """One LocationG / LocationReferenceG object (not a group):
    returns (shape, points) and fills parts."""
    if not isinstance(loc, dict):
        return None, []
    shape: Optional[str] = None
    pts: list[tuple[float, float]] = []
    if "locLinearLocation" in loc:
        shape = "line"
        pts = _walk_linear(loc.get("locLinearLocation"), parts)
    if "locPointLocation" in loc:
        p = _walk_point(loc.get("locPointLocation"), parts)
        if shape is None:
            shape = "point"
            pts = p
        elif not pts:
            pts = p
    _walk_single_road(loc.get("locSingleRoadLinearLocation"), parts)
    return shape, pts


def parse_location(record: dict) -> dict:
    """Read a record's locationReference.

    Returns a dict with:
      shape: "point" | "line" | "group" | None
      points: [(lon, lat), ...] (one point, a polyline, or all group polylines
              concatenated: the joins between members are NOT carriageway)
      lines: every polyline on its own: [] for a point, one list for a line,
              one list per member for a group
      lat, lon: representative position (the point, or the vertex at the
              middle of `points` by count), or None
      road: the roadName when every locLinearElementByCode agrees on one;
              None when none or several different roads are named
      road_names: every distinct roadName found
      direction: the normalised direction when every readable direction
              field agrees on one; None when none is readable or when they
              differ (a group with one member per carriageway, eastBound +
              westBound, is None, never "both": "both" is only said when the
              feed itself says bothWays/allDirections)
      direction_raw: the feed's own first direction value
      directions_raw: every distinct raw direction value, in feed order
      carriageways: [carriageway value, ...]
      lanes: [{number, usage, status, direction, carriageway}, ...]
      lanes_restricted, lanes_operational: ints or None
      location_description: the feed's locationDescription text (never used
              for road or direction), or None
    """
    parts = _LocParts()
    loc = record.get("locationReference") if isinstance(record, dict) else None
    shape: Optional[str] = None
    points: list[tuple[float, float]] = []
    lines: list[list[tuple[float, float]]] = []
    if isinstance(loc, dict):
        group = loc.get("locLocationGroupByList")
        if isinstance(group, dict):
            shape = "group"
            for member in _as_list(group.get("locationContainedInGroup")):
                _, pts = _walk_location(member, parts)
                lines.append(pts)
                points.extend(pts)
        else:
            shape, points = _walk_location(loc, parts)
            if shape == "line" and points:
                lines = [list(points)]
    rep = points[len(points) // 2] if points else None
    normalised: list[str] = []
    for d in parts.directions_raw:
        n = normalise_direction(d)
        if n is not None and n not in normalised:
            normalised.append(n)
    direction = normalised[0] if len(normalised) == 1 else None
    road = parts.road_names[0] if len(parts.road_names) == 1 else None
    return {
        "shape": shape,
        "points": points,
        "lines": lines,
        "lat": rep[1] if rep else None,
        "lon": rep[0] if rep else None,
        "road": road,
        "road_names": list(parts.road_names),
        "direction": direction,
        "direction_raw": parts.directions_raw[0] if parts.directions_raw else None,
        "directions_raw": list(parts.directions_raw),
        "carriageways": list(parts.carriageways),
        "lanes": list(parts.lanes),
        "lanes_restricted": parts.lanes_restricted,
        "lanes_operational": parts.lanes_operational,
        "location_description": parts.descriptions[0] if parts.descriptions else None,
    }


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _detailed_cause_values(dct: Any) -> list[str]:
    """detailedCauseType is a dict of one key (vehicleObstructionType,
    roadMaintenanceType, ...) whose value is a string, a list of strings, or
    an enum object {"value": ...}. Return the values found, in order."""
    if not isinstance(dct, dict):
        return _enum_values(dct)
    out: list[str] = []
    for _key, value in dct.items():
        out.extend(_enum_values(value))
    return out


def classify(record: dict, record_type_key: Optional[str] = None) -> tuple[str, Optional[str], Optional[str]]:
    """(kind, cause_type, detailed_cause) for a record.

    kind is "breakdown" when causeType == vehicleObstruction and the detailed
    value is in BREAKDOWN_CAUSES; "accident" when causeType == accident;
    otherwise "lane_closure". detailed_cause is the detailed value (several
    values are joined with ","), or None.

    When a record has no "cause" block at all but its DATEX II record type is
    itself a cause class (sitAccident, sitVehicleObstruction, ...), that class
    name is the cause and the record's own typed field (accidentType,
    vehicleObstructionType, ...) is the detail. Nothing is read from free text.
    """
    cause = record.get("cause") if isinstance(record, dict) else None
    cause_type: Optional[str] = None
    detailed: list[str] = []
    if isinstance(cause, dict):
        cause_type = _enum_value(cause.get("causeType"))
        detailed = _detailed_cause_values(cause.get("detailedCauseType"))
    if cause_type is None and record_type_key in RECORD_TYPE_CAUSES:
        implied_cause, field_name = RECORD_TYPE_CAUSES[record_type_key]
        cause_type = implied_cause
        if not detailed:
            detailed = _enum_values(record.get(field_name))
    if cause_type == "vehicleObstruction" and any(d in BREAKDOWN_CAUSES for d in detailed):
        kind = "breakdown"
    elif cause_type == "accident":
        kind = "accident"
    else:
        kind = "lane_closure"
    return kind, cause_type, (",".join(detailed) if detailed else None)


def _is_shoulder_lane(lane: dict) -> bool:
    usage = lane.get("usage")
    if usage in SHOULDER_LANE_USAGES:
        return True
    return lane.get("carriageway") in SHOULDER_CARRIAGEWAYS


def position_from_location(loc: dict) -> str:
    """"in_lane" when numberOfLanesRestricted >= 1: NH's own definition
    counts running lanes only ("the hard shoulder is not normally considered
    a usable lane and would not be counted as being closed"), so a positive
    count means a running lane is affected whatever the lane list says.
    When that count is 0 or absent: "shoulder" when the only closed lane(s)
    are hard shoulder / emergency lane / lay-by / verge (or lie on a lay-by /
    emergency-area carriageway), "in_lane" when any closed lane is a running
    lane, otherwise "unknown"."""
    restricted = loc.get("lanes_restricted")
    if isinstance(restricted, int) and restricted >= 1:
        return "in_lane"
    lanes = loc.get("lanes") or []
    closed = [ln for ln in lanes if (ln.get("status") or "").lower() == "closed"]
    if closed and all(_is_shoulder_lane(ln) for ln in closed):
        return "shoulder"
    if closed:
        return "in_lane"
    return "unknown"


def position(record: dict) -> str:
    """Report.position for a record: see position_from_location()."""
    return position_from_location(parse_location(record))


# --------------------------------------------------------------------------
# Payload walking and Report building
# --------------------------------------------------------------------------

def _situations(payload: Any) -> list[dict]:
    """Accept the full response ({"D2Payload": {...}}), the inner D2Payload,
    or a list of either (several pages)."""
    if payload is None:
        return []
    if isinstance(payload, list):
        out: list[dict] = []
        for page in payload:
            out.extend(_situations(page))
        return out
    if not isinstance(payload, dict):
        return []
    inner = payload.get("D2Payload", payload)
    if not isinstance(inner, dict):
        return []
    return [s for s in _as_list(inner.get("situation")) if isinstance(s, dict)]


def iter_records(payload: Any) -> Iterator[tuple[Optional[str], Optional[str], str, dict]]:
    """Yield (situation_id, situation_version_time, record_type_key, record)
    for every key of every situationRecord entry of every situation."""
    for sit in _situations(payload):
        sit_id = _str(sit.get("idG"))
        sit_version = _str(sit.get("situationVersionTime"))
        for entry in _as_list(sit.get("situationRecord")):
            if not isinstance(entry, dict):
                continue
            for key, record in entry.items():
                if isinstance(record, dict):
                    yield sit_id, sit_version, str(key), record


class NoCoordinates(ValueError):
    """The record has no usable lat/lon; parse_payload counts and skips it."""


def to_report(situation_id: Optional[str], situation_version_time: Optional[str],
              record_type_key: str, record: dict, index: int = 0) -> Report:
    """Build the common Report from one record. Raises NoCoordinates when the
    record carries no usable coordinates.

    reported_at is the earlier of validity.overallStartTime and
    situationRecordCreationTime (overallStartTime is a scheduled start for a
    planned or future record, and a scheduled start must never read as
    "first reported"); if even that lies after situationRecordVersionTime,
    only the creation time is trusted. The raw values are kept in
    extra["start_time"] / extra["creation_time"] for the probe to compare.

    Report.line holds every vertex of the location (for a group: all members
    concatenated, so the joins between members are not carriageway);
    extra["lines"] holds each polyline separately for drawing or
    distance-to-line work."""
    loc = parse_location(record)
    if loc["lat"] is None or loc["lon"] is None:
        raise NoCoordinates(f"NH record {record.get('idG') or situation_id!r} has no coordinates")
    kind, cause_type, detailed_cause = classify(record, record_type_key)

    record_id = _str(record.get("idG"))
    source_id = record_id or f"{situation_id or 'nh'}/{index}"

    validity = _as_dict(record.get("validity"))
    spec = _as_dict(validity.get("validityTimeSpecification"))
    start_time = parse_iso(spec.get("overallStartTime"))
    created = parse_iso(record.get("situationRecordCreationTime"))
    version = parse_iso(record.get("situationRecordVersionTime"))
    if start_time is not None and created is not None:
        reported_at = min(start_time, created)
    else:
        reported_at = start_time or created
    if reported_at is not None and version is not None and reported_at > version:
        reported_at = created

    description = None
    for c in _as_list(record.get("generalPublicComment")):
        text = _str(c.get("comment")) if isinstance(c, dict) else _str(c)
        if text:
            description = text
            break

    source_block = _as_dict(record.get("source"))

    extra = {
        "record_type": record_type_key,
        "management_type": _enum_value(record.get("roadOrCarriagewayOrLaneManagementType")),
        "validity_status": _str(validity.get("validityStatus")),
        "cause_type": cause_type,
        "detailed_cause": detailed_cause,
        "source_identification": _str(source_block.get("sourceIdentification")),
        "carriageways": loc["carriageways"],
        "lanes": loc["lanes"],
        "lanes_restricted": loc["lanes_restricted"],
        "lanes_operational": loc["lanes_operational"],
        "location_description": loc["location_description"],
        "version_time": _str(record.get("situationRecordVersionTime")),
        "creation_time": _str(record.get("situationRecordCreationTime")),
        "start_time": _str(spec.get("overallStartTime")),
        "end_time": _str(spec.get("overallEndTime")),
        "probability": _enum_value(record.get("probabilityOfOccurrence")),
        "shape": loc["shape"],
        "road_names": list(loc["road_names"]),
        "direction_raw": loc["direction_raw"],
        "directions_raw": list(loc["directions_raw"]),
        "lines": [[list(p) for p in member] for member in loc["lines"]],
        "situation_id": situation_id,
        "situation_version_time": situation_version_time,
    }
    return Report(
        source="nh",
        source_id=source_id,
        kind=kind,
        lat=float(loc["lat"]),
        lon=float(loc["lon"]),
        road=loc["road"],
        direction=loc["direction"],
        position=position_from_location(loc),
        description=description,
        from_place=None,
        to_place=None,
        reported_at=reported_at,
        n_reports=None,
        extra=extra,
        line=list(loc["points"]) if loc["shape"] != "point" else [],
    )


@dataclass
class ParseResult:
    reports: list[Report] = field(default_factory=list)
    n_records: int = 0
    n_skipped_no_coords: int = 0
    n_errors: int = 0
    errors: list[str] = field(default_factory=list)


def parse_payload_detailed(payload: Any) -> ParseResult:
    """Parse every record of a payload (or list of pages). Records without
    coordinates are skipped and counted; a record that fails for any other
    reason is counted as an error and does not stop the rest."""
    result = ParseResult()
    per_situation: Counter = Counter()
    for sit_id, sit_version, key, record in iter_records(payload):
        result.n_records += 1
        index = per_situation[sit_id]
        per_situation[sit_id] += 1
        try:
            result.reports.append(to_report(sit_id, sit_version, key, record, index))
        except NoCoordinates as e:
            result.n_skipped_no_coords += 1
            log.info("NH: skipped (no coordinates): %s", e)
        except Exception as e:  # one bad record must not lose the page
            result.n_errors += 1
            msg = f"{key} {record.get('idG') or sit_id}: {type(e).__name__}: {e}"
            result.errors.append(msg)
            log.warning("NH: could not parse record %s", msg)
    return result


def parse_payload(payload: Any) -> list[Report]:
    """All Reports in a payload (or list of pages); see parse_payload_detailed
    for the skip/error counts."""
    return parse_payload_detailed(payload).reports


_MISSING = "(missing)"


def distinct_values(payload: Any) -> dict[str, Counter]:
    """Counters of every distinct value seen, for the probe's log: cause_type,
    detailed_cause, management_type, validity_status, record_type, road_name,
    direction (raw), direction_normalised, carriageway, lane_usage,
    lane_status, shape, source_identification, probability, kind, position.
    A record that cannot be read is counted under "errors" (by record type
    and exception name) and does not lose the rest of the summary."""
    keys = ("cause_type", "detailed_cause", "management_type", "validity_status",
            "record_type", "road_name", "direction", "direction_normalised",
            "carriageway", "lane_usage", "lane_status", "shape",
            "source_identification", "probability", "kind", "position", "errors")
    out: dict[str, Counter] = {k: Counter() for k in keys}

    def bump(counter: str, value: Any) -> None:
        out[counter][value if value is not None else _MISSING] += 1

    for sit_id, _sit_version, key, record in iter_records(payload):
        try:
            _count_record(out, bump, key, record)
        except Exception as e:  # one bad record must not lose the summary
            out["errors"][f"{key}: {type(e).__name__}"] += 1
            log.warning("NH: could not summarise record %s %s: %s: %s",
                        key, record.get("idG") or sit_id, type(e).__name__, e)
    return out


def _count_record(out: dict[str, Counter], bump: Any, key: str, record: dict) -> None:
    """distinct_values() body for one record."""
    kind, cause_type, _detailed = classify(record, key)
    cause = _as_dict(record.get("cause"))
    detailed_list = _detailed_cause_values(cause.get("detailedCauseType"))
    if not detailed_list and key in RECORD_TYPE_CAUSES:
        detailed_list = _enum_values(record.get(RECORD_TYPE_CAUSES[key][1]))
    loc = parse_location(record)
    validity = _as_dict(record.get("validity"))
    source_block = _as_dict(record.get("source"))

    bump("record_type", key)
    bump("kind", kind)
    bump("cause_type", cause_type)
    if detailed_list:
        for d in detailed_list:
            bump("detailed_cause", d)
    else:
        bump("detailed_cause", None)
    bump("management_type", _enum_value(record.get("roadOrCarriagewayOrLaneManagementType")))
    bump("validity_status", _str(validity.get("validityStatus")))
    bump("probability", _enum_value(record.get("probabilityOfOccurrence")))
    bump("source_identification", _str(source_block.get("sourceIdentification")))
    bump("shape", loc["shape"])
    bump("position", position_from_location(loc))
    if loc["road_names"]:
        for r in loc["road_names"]:
            bump("road_name", r)
    else:
        bump("road_name", None)
    if loc["directions_raw"]:
        for d in loc["directions_raw"]:
            bump("direction", d)
    else:
        bump("direction", None)
    bump("direction_normalised", loc["direction"])
    if loc["carriageways"]:
        for c in loc["carriageways"]:
            bump("carriageway", c)
    else:
        bump("carriageway", None)
    for ln in loc["lanes"]:
        bump("lane_usage", ln.get("usage"))
        bump("lane_status", ln.get("status"))
