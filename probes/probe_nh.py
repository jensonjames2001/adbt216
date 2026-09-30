"""National Highways probe: what does the live "Road and Lane Closures"
unplanned feed carry for our box, and how does the endpoint behave?

Run from the repo root:

    .venv/bin/python probes/probe_nh.py                          # one sample: unplanned closures, last 6 h, follows pages
    .venv/bin/python probes/probe_nh.py --repeat 3 --interval 600
    .venv/bin/python probes/probe_nh.py --no-dates               # also one call without a date window
    .venv/bin/python probes/probe_nh.py --xml-check              # also one call without the JSON header
    .venv/bin/python probes/probe_nh.py --test-30-days           # also one call with a 31-day window
    .venv/bin/python probes/probe_nh.py --parse-fixture fixtures/nh/doc_sample_incident.json   # offline, no key

Needs NH_API_KEY in .env, except with --parse-fixture (offline: parses the
saved file, prints the record table and every distinct value, exit 0).

Exit codes: 0 = ran to the end, 1 = a request failed or the answer could not
be read, 3 = NH_API_KEY missing, 4 = the key was rejected (HTTP 401/403).

Requests: one per sample plus one per extra page (the x-next header), plus
one for each of --no-dates, --xml-check and --test-30-days. Never more than
--max-calls-per-min calls in any minute (default 5; the key allows 10), and
never more than --max-requests per run (default 40). No retries: a failure
is reported, not retried, and after an HTTP 429 the optional checks are
skipped and the next sample waits as long as the API asked. The x-next
header is followed only when it points at the National Highways host over
https (the key goes with every request, so it is never sent anywhere else).

What is saved (fixtures/nh/live/<timestamp>/): every page as it came back
(the feed carries no personal data), request meta with the key replaced by
***, sampleNN.records.json (the parsed reports; fixture.records.json with
--parse-fixture), findings.md and summary.json. fixtures/nh/live/state.json
remembers the ids seen by each sample so the next sample (or the next run)
can say which records persisted, vanished or are new.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urljoin, urlsplit

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from probes import common  # noqa: E402
from probes.common import (  # noqa: E402
    EXIT_FAILED, EXIT_OK, Findings, RequestBudget, explain_exception, london,
    missing_key, now_utc, redacted_url, save_response, secret, status_line,
    timed_request, write_json,
)
from rgalerts.models import Report  # noqa: E402
from rgalerts.roads import RoadIndex  # noqa: E402
from rgalerts.sources import nh_parse as nh  # noqa: E402

EXIT_KEY_REJECTED = 4

DEFAULT_WINDOW_HOURS = 6.0
DEFAULT_MAX_CALLS_PER_MIN = 5
NH_KEY_LIMIT_PER_MIN = 10          # what the portal says the key allows; never go past it
DEFAULT_MAX_REQUESTS = 40
DEFAULT_INTERVAL_S = 600
DEFAULT_MAX_PAGES = 20             # per fetch; a safety net against an x-next loop
LONG_WINDOW_DAYS = 31              # --test-30-days
XML_CHECK_BODY_BYTES = 2048
ERROR_BODY_CHARS = 500
ON_ROAD_MAX_M = 100.0
COMMENT_CHARS = 80
MAX_TABLE_ROWS = 60                # the national table; the in-box table is never cut
STATE_KEEP_SAMPLES = 100
DOCUMENTED_RECORD_TYPE = "sitRoadOrCarriagewayOrLaneManagement"
PARSER_POSLIST_ORDER = "latlon"    # what nh_parse assumes (documented, fixture-confirmed)
_NH_PARTS = urlsplit(nh.NH_BASE)
NH_HOST = (_NH_PARTS.hostname or "").lower()   # x-next is followed only here, over https
NH_PATH = _NH_PARTS.path                       # ...and only for this endpoint's path


# --------------------------------------------------------------------------
# Pure helpers (importable, no network)
# --------------------------------------------------------------------------

class RateLimiter:
    """At most ``max_per_min`` calls in any 60 s window, enforced with the
    timestamps of the last calls. ``wait()`` sleeps when needed and returns
    the seconds slept. Every call counts, pagination included."""

    def __init__(self, max_per_min: int, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep, window_s: float = 60.0) -> None:
        self.max = int(max_per_min)
        if self.max < 1:
            raise ValueError("max calls per minute must be at least 1")
        self.window = float(window_s)
        self.clock = clock
        self.sleeper = sleeper
        self.recent: deque[float] = deque()
        self.history: list[float] = []
        self.total_slept = 0.0

    def _drop_old(self, now: float) -> None:
        while self.recent and now - self.recent[0] >= self.window:
            self.recent.popleft()

    def wait(self) -> float:
        now = self.clock()
        self._drop_old(now)
        slept = 0.0
        if len(self.recent) >= self.max:
            slept = self.window - (now - self.recent[0]) + 0.1
            self.sleeper(slept)
            self.total_slept += slept
            now = self.clock()
            self._drop_old(now)
        self.recent.append(now)
        self.history.append(now)
        return slept

    def max_in_any_window(self) -> int:
        """The busiest 60 s of the run so far, for the findings."""
        best = 0
        stamps = self.history
        for i, t in enumerate(stamps):
            n = sum(1 for u in stamps[i:] if u - t < self.window)
            best = max(best, n)
        return best


def retry_seconds(data: Any, headers: Any = None) -> Optional[float]:
    """The wait a 429 asks for: "Try again in 23 seconds" in the JSON
    message, else a Retry-After header, else None."""
    msg = data.get("message") if isinstance(data, dict) else None
    if isinstance(msg, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*sec", msg, re.IGNORECASE)
        if m:
            return float(m.group(1))
    if headers is not None:
        try:
            ra = headers.get("retry-after")
        except AttributeError:
            ra = None
        if ra:
            try:
                return float(ra)
            except ValueError:
                return None
    return None


def resolve_next_url(next_header: str) -> tuple[Optional[str], Optional[str]]:
    """(url, None) when the x-next header may be followed, else (None, why).

    The subscription key travels with every request, so a next-page URL is
    followed only when it is https, on the National Highways host and under
    the closures path. A relative value is resolved against the endpoint. A
    value that cannot be parsed at all is refused rather than guessed at."""
    try:
        url = urljoin(nh.NH_BASE, str(next_header).strip())
        parts = urlsplit(url)
    except ValueError:
        return None, "it is not a valid URL"
    if parts.scheme.lower() != "https":
        return None, f"it is {parts.scheme or 'no scheme'}, not https"
    host = (parts.hostname or "").lower()
    if host != NH_HOST:
        return None, f"it points at {host or '(no host)'}, not {NH_HOST}"
    if parts.path != NH_PATH:
        return None, f"its path is {parts.path or '/'}, not {NH_PATH}"
    return url, None


def url_key(url: str) -> tuple:
    """A comparable form of a URL: scheme, host, path and the DECODED query
    pairs, so ':' and '%3A' in a date-time compare equal."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return (str(url),)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.path,
            tuple(sorted(parse_qsl(parts.query, keep_blank_values=True))))


def age_text(seconds: float) -> str:
    """'45 s', '3 min', '2 h 5 min', '2 days 3 h': the gap between two samples."""
    total = int(abs(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} day{'s' if days != 1 else ''} {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min"
    return f"{total} s"


def dedupe_reports(reports: list[Report]) -> tuple[list[Report], int]:
    """Keep the first report per source_id. A record can come back on two
    pages when the feed changes between page fetches; it must not count
    twice. Returns (unique reports, number dropped)."""
    seen: set[str] = set()
    unique: list[Report] = []
    dropped = 0
    for r in reports:
        if r.source_id in seen:
            dropped += 1
            continue
        seen.add(r.source_id)
        unique.append(r)
    return unique, dropped


def error_message(data: Any, content: bytes, limit: int = ERROR_BODY_CHARS) -> str:
    """NH errors are JSON {"message": ...}; fall back to the body's start."""
    if isinstance(data, dict):
        for key in ("message", "Message", "error", "detail"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    text = (content or b"")[:limit].decode("utf-8", errors="replace").replace("\n", " ").strip()
    return text or "(empty body)"


def body_kind(content_type: str, content: bytes) -> str:
    """'json' | 'xml' | 'html' | 'text' | 'empty' from the content-type and
    the first bytes; used to spot an XML answer when JSON was asked for."""
    if not content:
        return "empty"
    ct = (content_type or "").lower()
    head = content.lstrip()[:64].lower()
    if "json" in ct or head[:1] in (b"{", b"["):
        return "json"
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or "html" in ct:
        return "html"
    if "xml" in ct or head.startswith(b"<?xml") or head.startswith(b"<"):
        return "xml"
    return "text"


def situations_in(page: Any) -> int:
    """Number of situations in one page (full response or inner D2Payload)."""
    if not isinstance(page, dict):
        return 0
    inner = page.get("D2Payload", page)
    if not isinstance(inner, dict):
        return 0
    s = inner.get("situation")
    if isinstance(s, list):
        return len(s)
    return 1 if isinstance(s, dict) else 0


def publication_time(page: Any) -> Optional[datetime]:
    if not isinstance(page, dict):
        return None
    inner = page.get("D2Payload", page)
    if not isinstance(inner, dict):
        return None
    return nh.parse_iso(inner.get("publicationTime"))


def find_key(obj: Any, key: str) -> list[Any]:
    """Every value stored under ``key`` anywhere inside obj (dicts and lists)."""
    found: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            found.extend(find_key(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(find_key(v, key))
    return found


def poslist_pairs(poslist: Any, srs_dimension: Any = 2) -> list[tuple[float, float]]:
    """The raw (first, second) number pairs of a posList, in the feed's own
    order, so the order can be checked independently of the parser."""
    if poslist is None:
        return []
    text = " ".join(str(x) for x in poslist) if isinstance(poslist, (list, tuple)) else str(poslist)
    try:
        nums = [float(t) for t in text.replace(",", " ").split()]
    except ValueError:
        return []
    try:
        dim = max(2, int(srs_dimension)) if srs_dimension is not None else 2
    except (TypeError, ValueError):
        dim = 2
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - dim + 1, dim)]


def poslist_order_check(pages: Any) -> dict[str, Any]:
    """Run the parser's order-detecting helper on every posList in the
    payload and report where it disagrees with the parser's assumed order."""
    counts: Counter = Counter()
    srs_names: Counter = Counter()
    disagreements: list[dict[str, Any]] = []
    n_line_strings = 0
    for sit_id, _ver, _key, record in nh.iter_records(pages):
        rec_id = record.get("idG") or sit_id
        for gml in find_key(record.get("locationReference"), "locGmlLineString"):
            if not isinstance(gml, dict):
                continue
            n_line_strings += 1
            srs_names[str(gml.get("srsName") or "(missing)")] += 1
            pairs = poslist_pairs(gml.get("posList"), gml.get("srsDimension", 2))
            if not pairs:
                counts["empty"] += 1
                continue
            order = nh.poslist_order(pairs)
            counts[order or "undetermined"] += 1
            if order != PARSER_POSLIST_ORDER:
                disagreements.append({
                    "record": rec_id, "detected": order or "undetermined",
                    "first_pair": list(pairs[0]) if pairs else None,
                })
    return {
        "line_strings": n_line_strings,
        "counts": dict(counts),
        "srs_names": dict(srs_names),
        "disagreements": disagreements,
        "all_as_parser_assumes": not disagreements,
    }


def point_location_check(pages: Any) -> dict[str, Any]:
    """How many records carry a locPointLocation (anywhere in their
    locationReference) and how many of those have real coordinates."""
    n_records_with_point = 0
    n_with_coordinates = 0
    ids: list[str] = []
    for sit_id, _ver, _key, record in nh.iter_records(pages):
        points = find_key(record.get("locationReference"), "locPointLocation")
        if not points:
            continue
        n_records_with_point += 1
        ids.append(str(record.get("idG") or sit_id))
        if any(find_key(p, "pointCoordinates") for p in points):
            n_with_coordinates += 1
    return {"records_with_point": n_records_with_point,
            "records_with_point_coordinates": n_with_coordinates, "ids": ids}


def in_box(report: Report, box: Any) -> bool:
    """Representative point inside the box, or any polyline vertex inside."""
    if box.contains(report.lat, report.lon):
        return True
    return any(box.contains(lat, lon) for lon, lat in (report.line or []))


def road_match(index: Optional[RoadIndex], report: Report, roads: list[str],
               max_m: float = ON_ROAD_MAX_M) -> Optional[dict]:
    """The nearest configured road (per RoadIndex) within max_m of the
    representative point or of any polyline vertex, or None."""
    if index is None:
        return None
    candidates = [(report.lat, report.lon)] + [(lat, lon) for lon, lat in (report.line or [])]
    best: Optional[dict] = None
    for lat, lon in candidates:
        hit = index.nearest(lat, lon, max_m=max_m, roads=roads)
        if hit and (best is None or hit["distance_m"] < best["distance_m"]):
            best = hit
    return best


def truncate(text: Optional[str], n: int = COMMENT_CHARS) -> str:
    if not text:
        return "-"
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def london_dt(dt: Optional[datetime]) -> str:
    """'30 Sep 2026 14:05' in Europe/London; 'unknown' when there is no time.
    The year is kept: fixtures and state.json comparisons span years."""
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(common.LONDON).strftime("%d %b %Y %H:%M")


def lanes_closed_text(extra: dict) -> str:
    lanes = extra.get("lanes") or []
    closed = [ln for ln in lanes if str(ln.get("status") or "").lower() == "closed"]
    if closed:
        labels: list[str] = []
        for ln in closed:
            label = str(ln.get("usage") or ln.get("number") or "?")
            if label not in labels:
                labels.append(label)
        return ", ".join(labels)
    restricted = extra.get("lanes_restricted")
    if isinstance(restricted, int) and restricted > 0:
        return f"{restricted} restricted"
    return "-"


def cause_text(extra: dict) -> str:
    cause = extra.get("cause_type") or "unknown"
    detailed = extra.get("detailed_cause")
    return f"{cause}/{detailed}" if detailed else cause


def on_road_text(hit: Optional[dict], index: Optional[RoadIndex]) -> str:
    if index is None:
        return "n/a (no roads.json)"
    if hit is None:
        return f"no (none within {ON_ROAD_MAX_M:.0f} m)"
    return f"{hit['road']} ({hit['distance_m']:.0f} m)"


TABLE_HEADER = ["source_id", "kind", "road", "direction", "management", "cause/detailed",
                "position", "lanes closed", "validity", "start (London)", "comment",
                "lat,lon", "on configured road"]


def table_row(report: Report, hit: Optional[dict], index: Optional[RoadIndex]) -> list[Any]:
    e = report.extra or {}
    return [
        report.source_id,
        report.kind,
        report.road or "unknown",
        report.direction or "unknown",
        e.get("management_type") or "unknown",
        cause_text(e),
        report.position,
        lanes_closed_text(e),
        e.get("validity_status") or "unknown",
        london_dt(report.reported_at),
        truncate(report.description),
        f"{report.lat:.5f},{report.lon:.5f}",
        on_road_text(hit, index),
    ]


def distinct_rows(dv: dict[str, Counter]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for field, counter in dv.items():
        for value, n in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            rows.append([field, value, n])
    return rows


def merge_distinct(into: dict[str, Counter], dv: dict[str, Counter]) -> None:
    for field, counter in dv.items():
        into.setdefault(field, Counter()).update(counter)


def time_span(reports: list[Report]) -> tuple[Optional[datetime], Optional[datetime]]:
    times = [r.reported_at for r in reports if r.reported_at is not None]
    return (min(times), max(times)) if times else (None, None)


def analyse(pages: list[Any], box: Any, roads: list[str], index: Optional[RoadIndex]) -> dict[str, Any]:
    """Every number the findings report for one payload (or list of pages)."""
    result = nh.parse_payload_detailed(pages)
    dv = nh.distinct_values(pages)
    reports, n_duplicates = dedupe_reports(result.reports)
    boxed = [r for r in reports if in_box(r, box)]
    hits = {r.source_id: road_match(index, r, roads) for r in boxed}
    on_roads = [r for r in boxed if hits.get(r.source_id)]
    kinds_boxed = Counter(r.kind for r in boxed)
    kinds_on_roads = Counter(r.kind for r in on_roads)
    road_agreement = Counter()
    for r in on_roads:
        nh_road = (r.road or "").upper()
        osm_road = hits[r.source_id]["road"]
        road_agreement["agree" if nh_road == osm_road else ("nh_unknown" if not nh_road else "disagree")] += 1
    other_types = {k: n for k, n in dv["record_type"].items() if k != DOCUMENTED_RECORD_TYPE}
    breakdown_causes = {d: n for d, n in dv["detailed_cause"].items() if d in nh.BREAKDOWN_CAUSES}
    first, last = time_span(reports)
    return {
        "parse": result,
        "distinct": dv,
        "reports": reports,
        "in_box": boxed,
        "hits": hits,
        "on_roads": on_roads,
        "n_situations": sum(situations_in(p) for p in pages),
        "n_records": result.n_records,
        "n_with_coordinates": len(reports),
        "n_duplicate_ids": n_duplicates,
        "n_skipped_no_coords": result.n_skipped_no_coords,
        "n_errors": result.n_errors,
        "n_in_box": len(boxed),
        "n_on_roads": len(on_roads),
        "kinds_in_box": dict(kinds_boxed),
        "kinds_on_roads": dict(kinds_on_roads),
        "road_agreement": dict(road_agreement),
        "other_record_types": other_types,
        "breakdown_causes": breakdown_causes,
        "vehicle_obstruction_records": dv["cause_type"].get("vehicleObstruction", 0),
        "poslist": poslist_order_check(pages),
        "points": point_location_check(pages),
        "earliest_start": first,
        "latest_start": last,
        "source_ids": sorted({r.source_id for r in reports}),
        "in_box_ids": sorted({r.source_id for r in boxed}),
    }


# --------------------------------------------------------------------------
# State between samples
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("samples"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"samples": []}


def compare_ids(previous: Optional[dict], current: dict) -> Optional[dict[str, Any]]:
    """persisted / vanished / new between two samples, for all ids and for
    the in-box ids. None when there is no previous sample."""
    if not previous:
        return None
    out: dict[str, Any] = {"previous_at": previous.get("at"), "previous_at_london": previous.get("at_london")}
    for field in ("source_ids", "in_box_ids"):
        prev = set(previous.get(field) or [])
        cur = set(current.get(field) or [])
        out[field] = {
            "persisted": len(prev & cur),
            "vanished": len(prev - cur),
            "new": len(cur - prev),
            "vanished_ids": sorted(prev - cur)[:50],
            "new_ids": sorted(cur - prev)[:50],
        }
    return out


def update_state(state: dict[str, Any], sample: dict[str, Any], keep: int = STATE_KEEP_SAMPLES) -> Optional[dict]:
    samples = state.setdefault("samples", [])
    previous = samples[-1] if samples else None
    comparison = compare_ids(previous, sample)
    samples.append(sample)
    del samples[:-keep]
    return comparison


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def fetch_pages(
    client: Any, url: str, params: Optional[dict], headers: dict, *,
    budget: RequestBudget, limiter: RateLimiter, findings: Findings, outdir: Path,
    stem: str, follow: bool = True, max_pages: int = DEFAULT_MAX_PAGES,
    max_body_bytes: Optional[int] = None, expect_json: bool = True,
) -> dict[str, Any]:
    """GET the first page and, when follow is set, every x-next page after
    it. Rate-limited and budgeted; never retries. Returns a dict describing
    what happened; 'pages' holds the parsed JSON pages."""
    out: dict[str, Any] = {
        "pages": [], "statuses": [], "requests": 0, "elapsed_s": 0.0,
        "x_next_seen": False, "stopped": None, "error": None,
        "key_rejected": False, "rate_limited": False, "retry_after_s": None,
        "body_kinds": [], "content_types": [], "situations_per_page": [],
        "waited_s": 0.0, "last_status": None,
    }
    next_url: Optional[str] = None
    sent: set[tuple] = set()          # url_key() of every URL actually requested
    page = 0
    while True:
        page += 1
        if page > max_pages:
            out["stopped"] = f"stopped after {max_pages} pages (--max-pages)"
            break
        if budget.total >= budget.max_requests:
            out["stopped"] = (f"the per-run cap of {budget.max_requests} requests is reached; "
                              f"page {page} was not fetched")
            break
        slept = limiter.wait()
        if slept:
            out["waited_s"] += slept
            findings.add(f"  waited {slept:.0f} s to stay under {limiter.max} calls a minute")
        budget.count("nh")
        req_url = next_url or url
        req_params = None if next_url else params
        shown = str(common.httpx.URL(req_url, params=req_params)) if req_params else req_url
        findings.add(f"GET {redacted_url(shown)}" + (f"  (page {page})" if page > 1 else ""))
        try:
            response, took = timed_request(client, "GET", req_url, params=req_params, headers=headers)
        except common.httpx.HTTPError as exc:
            out["error"] = f"request failed: {explain_exception(exc)}"
            out["requests"] += 1
            findings.add(f"  ERROR: {out['error']}")
            break
        out["requests"] += 1
        out["elapsed_s"] += took
        sent.add(url_key(str(response.request.url)))
        status = response.status_code
        out["statuses"].append(status)
        out["last_status"] = status
        findings.add(f"  {status_line(response, took)}")
        save_response(outdir, f"{stem}_page{page:02d}", response, elapsed_s=took,
                      max_body_bytes=max_body_bytes)
        content_type = response.headers.get("content-type", "")
        kind = body_kind(content_type, response.content)
        out["content_types"].append(content_type)
        out["body_kinds"].append(kind)
        data: Any = None
        if kind == "json":
            try:
                data = response.json()
            except ValueError:
                data = None

        if status == 200:
            if kind != "json" or data is None:
                if kind == "json":
                    what = (f"the answer said it was JSON (content-type {content_type or '?'}) but could "
                            f"not be read as JSON; saved as {stem}_page{page:02d}")
                else:
                    what = (f"the answer was {kind}, not JSON (content-type {content_type or '?'}); "
                            f"saved as {stem}_page{page:02d}")
                if expect_json:
                    out["error"] = what
                    findings.add(f"  ERROR: {what}")
                else:
                    findings.add(f"  {what}")
                break
            out["pages"].append(data)
            n_sit = situations_in(data)
            out["situations_per_page"].append(n_sit)
            nxt = nh.next_page_url(response.headers)
            findings.add(f"  {n_sit} situation(s) on this page; x-next header: "
                         + (redacted_url(nxt) if nxt else "absent"))
            if nxt:
                out["x_next_seen"] = True
            if not follow:
                if nxt:
                    out["stopped"] = "more pages exist; not followed for this check"
                break
            if not nxt:
                break
            resolved, why_not = resolve_next_url(nxt)
            if resolved is None:
                out["stopped"] = (f"x-next not followed: {why_not} ({redacted_url(nxt)}). "
                                  "The key is only ever sent to National Highways.")
                findings.add(f"  {out['stopped']}")
                break
            if url_key(resolved) in sent:
                out["stopped"] = "x-next pointed back at a page already fetched (a loop); stopped"
                findings.add(f"  {out['stopped']}")
                break
            next_url = resolved
            continue

        msg = error_message(data, response.content)
        if status in (401, 403):
            out["key_rejected"] = True
            what = "key rejected" if status == 401 else "key not allowed for this API"
            findings.add(f"  {what} (HTTP {status}): {msg}")
            findings.add("  Check NH_API_KEY in .env: it is the subscription key from "
                         "developer.data.nationalhighways.co.uk, and the subscription must be approved.")
        elif status == 429:
            out["rate_limited"] = True
            wait = retry_seconds(data, response.headers)
            out["retry_after_s"] = wait
            findings.add(f"  HTTP 429: {msg}")
            findings.add("  The API asks us to wait " + (f"{wait:.0f} s" if wait is not None else "an unstated time")
                         + ". Not retrying (probes never retry).")
        elif status == 500:
            findings.add(f"  HTTP 500; body: {truncate(response.content.decode('utf-8', errors='replace'), ERROR_BODY_CHARS)}")
        else:
            findings.add(f"  HTTP {status}: {msg}")
        out["error"] = f"HTTP {status}: {msg}"
        break
    return out


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def report_analysis(findings: Findings, a: dict[str, Any], *, index: Optional[RoadIndex],
                    roads: list[str], label: str, stem: str, national_rows: int = MAX_TABLE_ROWS) -> None:
    """Print one payload's analysis: counts, distinct values, tables."""
    p = a["parse"]
    findings.section(f"{label}: records")
    findings.kv("Situations", a["n_situations"])
    findings.kv("Records nationally", a["n_records"])
    findings.kv("Records with coordinates", a["n_with_coordinates"])
    if a["n_duplicate_ids"]:
        findings.kv("Records repeated on a later page (same id, counted once)", a["n_duplicate_ids"])
    findings.kv("Records skipped (no coordinates)", a["n_skipped_no_coords"])
    findings.kv("Records that failed to parse", a["n_errors"])
    for err in p.errors[:10]:
        findings.add(f"  parse error: {err}")
    findings.kv("Earliest start time (London)", london_dt(a["earliest_start"]))
    findings.kv("Latest start time (London)", london_dt(a["latest_start"]))
    findings.kv("Records inside the box", a["n_in_box"])
    findings.kv("Records inside the box on " + "/".join(roads), a["n_on_roads"] if index else None)
    findings.kv("In-box kinds", a["kinds_in_box"] or {})
    findings.kv("On-road kinds", a["kinds_on_roads"] or {})
    if index and a["on_roads"]:
        findings.kv("NH roadName vs nearest OSM road", a["road_agreement"])
    findings.kv("Records with a cause of vehicleObstruction", a["vehicle_obstruction_records"])
    findings.kv("Breakdown-specific detailed causes seen", a["breakdown_causes"] or "none")
    findings.kv("Record type keys other than " + DOCUMENTED_RECORD_TYPE, a["other_record_types"] or "none")

    pts = a["points"]
    findings.kv("Records with a locPointLocation", pts["records_with_point"])
    findings.kv("...of which with pointCoordinates", pts["records_with_point_coordinates"])
    if pts["ids"]:
        findings.kv("Point-location record ids", pts["ids"][:20])

    pl = a["poslist"]
    findings.kv("posList line strings", pl["line_strings"])
    findings.kv("posList order detected (parser assumes latlon)", pl["counts"] or "none")
    findings.kv("posList srsName values", pl["srs_names"] or "none")
    if pl["disagreements"]:
        findings.add("WARNING: some posLists did not read as lat lon; the parser's assumed order needs checking:")
        findings.table([[d["record"], d["detected"], d["first_pair"]] for d in pl["disagreements"][:20]],
                       header=["record", "detected", "first pair"])
    else:
        findings.add("Every posList read as lat lon, as the parser assumes." if pl["line_strings"]
                     else "No posList in this payload.")

    findings.section(f"{label}: distinct values (every counter, in full)")
    findings.table(distinct_rows(a["distinct"]), header=["field", "value", "count"])

    findings.section(f"{label}: records inside the box")
    if index is None:
        findings.add("data/roads.json is missing, so the 'on configured road' column is n/a. "
                     "Run: .venv/bin/python scripts/fetch_junctions.py")
    rows = [table_row(r, a["hits"].get(r.source_id), index) for r in a["in_box"]]
    findings.table(rows, header=TABLE_HEADER)

    findings.section(f"{label}: all records with coordinates (first {national_rows})")
    rows = [table_row(r, None, None)[:-1] for r in a["reports"][:national_rows]]
    findings.table(rows, header=TABLE_HEADER[:-1])
    if len(a["reports"]) > national_rows:
        findings.add(f"... {len(a['reports']) - national_rows} more; the full list is in {stem}.records.json")


def write_records(outdir: Path, stem: str, a: dict[str, Any], box: Any) -> None:
    write_json(outdir / f"{stem}.records.json", {
        "written_at": common.iso(now_utc()),
        "box": {"west": box.west, "south": box.south, "east": box.east, "north": box.north},
        "n_records": a["n_records"],
        "in_box_ids": a["in_box_ids"],
        "on_road_ids": [r.source_id for r in a["on_roads"]],
        "reports": [r.to_dict() for r in a["reports"]],
    })


def load_road_index(cfg: dict, findings: Findings) -> Optional[RoadIndex]:
    rel = ((cfg.get("storage") or {}).get("roads")) or "data/roads.json"
    path = Path(rel)
    if not path.is_absolute():
        path = common.REPO_ROOT / path
    if not path.is_file():
        findings.add(f"Note: {common.display_path(path)} not found; the on-road check is skipped. "
                     "Run: .venv/bin/python scripts/fetch_junctions.py")
        return None
    try:
        index = RoadIndex.load(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        findings.add(f"Note: could not read {common.display_path(path)} ({exc}); the on-road check is skipped.")
        return None
    findings.kv("Road index", f"{len(index)} OSM ways for {', '.join(index.roads)} "
                              f"(Map data © OpenStreetMap contributors, ODbL 1.0)")
    return index


def load_fixture_pages(paths: list[str]) -> list[Any]:
    pages: list[Any] = []
    for p in paths:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            pages.extend(data)
        else:
            pages.append(data)
    return pages


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

def run_offline(args: Any, cfg: dict, outdir: Path) -> int:
    findings = Findings("National Highways probe (offline: parse a saved response)")
    findings.kv("Run started (UTC)", common.iso(now_utc()))
    findings.kv("Files", [common.display_path(p) for p in args.parse_fixture])
    findings.add("No request is sent in this mode.")
    box = cfg["box_obj"]
    roads = list(cfg.get("roads") or [])
    index = load_road_index(cfg, findings)
    try:
        pages = load_fixture_pages(args.parse_fixture)
    except (OSError, ValueError) as exc:
        findings.add(f"ERROR: could not read the file: {exc}")
        findings.write(outdir)
        return EXIT_FAILED
    a = analyse(pages, box, roads, index)
    report_analysis(findings, a, index=index, roads=roads, label="Fixture", stem="fixture")
    write_records(outdir, "fixture", a, box)
    findings.section("Requests")
    findings.kv("Requests sent", 0)
    findings.write(outdir)
    return EXIT_OK


def run_live(args: Any, cfg: dict, outdir: Path, key: str, notes: Optional[list[str]] = None) -> int:
    box = cfg["box_obj"]
    roads = list(cfg.get("roads") or [])
    findings = Findings("National Highways probe")
    started = now_utc()
    findings.kv("Run started (UTC)", common.iso(started))
    findings.kv("Run started (Europe/London)", london_dt(started))
    for note in notes or []:
        findings.add(f"Note: {note}")
    findings.kv("Endpoint", nh.NH_BASE)
    findings.kv("Headers sent", f"{nh.HEADER_KEY}: *** ; {nh.HEADER_MEDIA}: application/json ; {nh.HEADER_FORMAT}: DATEXII")
    findings.kv("Window (hours)", args.window_hours)
    findings.kv("Samples", f"{args.repeat} every {args.interval} s" if args.repeat > 1 else 1)
    findings.kv("Rate cap", f"{args.max_calls_per_min} calls a minute (the key allows {NH_KEY_LIMIT_PER_MIN})")
    findings.kv("Box (W,S,E,N)", f"{box.west}, {box.south}, {box.east}, {box.north}")
    findings.kv("Configured roads", roads)
    index = load_road_index(cfg, findings)

    extras = [name for name, on in (("--no-dates", args.no_dates), ("--xml-check", args.xml_check),
                                    ("--test-30-days", args.test_30_days)) if on]
    planned = args.repeat + len(extras)
    budget = RequestBudget(args.max_requests)
    try:
        budget.check(planned, f"first page of {args.repeat} sample(s)"
                     + (", plus " + ", ".join(extras) if extras else "")
                     + "; every extra page (x-next) is counted as it appears")
    except SystemExit as exc:
        # The plan is over the per-run cap: say so in findings.md and stop
        # before any request, rather than leaving an empty folder behind.
        findings.section("Result")
        findings.add(str(exc))
        findings.add("Nothing was sent. Exit code 1.")
        findings.write(outdir)
        return EXIT_FAILED
    limiter = RateLimiter(args.max_calls_per_min)
    headers = nh.headers(key)
    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    if state["samples"]:
        findings.kv("Previous samples in state.json", len(state["samples"]))

    merged_distinct: dict[str, Counter] = {}
    sample_rows: list[list[Any]] = []
    answers: dict[str, Any] = {
        "header_confirmed": False, "pages_max": 0, "x_next_seen": False,
        "breakdown_causes": Counter(), "vehicle_obstruction_records": 0, "point_records": 0,
        "other_record_types": Counter(), "poslist_disagreements": 0, "poslist_line_strings": 0,
        "in_box_total": 0, "on_roads_total": 0, "samples_done": 0, "samples_failed": 0,
        "no_dates": None, "xml_check": None, "test_30_days": None,
    }
    rc = EXIT_OK
    key_rejected = False
    rate_limited = False                  # any HTTP 429 in this run, pages included
    retry_after_s: Optional[float] = None  # the longest wait a 429 asked for

    def optional_checks() -> None:
        """--no-dates / --xml-check / --test-30-days once, unless a 429 came first."""
        nonlocal rate_limited, retry_after_s
        if not extras:
            return
        if rate_limited:
            findings.add("Skipping " + ", ".join(extras) + " after the 429; run again later.")
            for name, field in (("--no-dates", "no_dates"), ("--xml-check", "xml_check"),
                                ("--test-30-days", "test_30_days")):
                if name in extras:
                    answers[field] = {"status": None, "error": "an HTTP 429 came earlier in this run"}
            return
        limited, wait = run_optional_checks(args, answers, client, headers, budget, limiter,
                                            findings, outdir, box)
        if limited:
            rate_limited = True
            if wait is not None:
                retry_after_s = max(retry_after_s or 0.0, wait)

    try:
        with common.http_client() as client:
            for i in range(args.repeat):
                n = i + 1
                at = now_utc()
                findings.section(f"Sample {n} of {args.repeat} at {london(at)} London ({common.iso(at)})")
                params = nh.build_params(at, args.window_hours)
                findings.kv("Query", {k: v for k, v in params.items()})
                stem = f"sample{n:02d}"
                f = fetch_pages(client, nh.NH_BASE, params, headers, budget=budget, limiter=limiter,
                                findings=findings, outdir=outdir, stem=stem, max_pages=args.max_pages)
                if f["key_rejected"]:
                    key_rejected = True
                    break
                if f["rate_limited"]:
                    rate_limited = True
                    if f["retry_after_s"] is not None:
                        retry_after_s = max(retry_after_s or 0.0, f["retry_after_s"])
                if f["pages"]:
                    answers["header_confirmed"] = True
                    answers["pages_max"] = max(answers["pages_max"], len(f["pages"]))
                    answers["x_next_seen"] = answers["x_next_seen"] or f["x_next_seen"]
                    findings.kv("Pages fetched", len(f["pages"]))
                    findings.kv("Situations per page", f["situations_per_page"])
                    pub = publication_time(f["pages"][0])
                    if pub is not None:
                        findings.kv("publicationTime (London)", f"{london_dt(pub)} "
                                    f"({(at - pub).total_seconds():.0f} s before the request)")
                if f["stopped"]:
                    findings.add(f"Pagination: {f['stopped']}")
                if f["error"]:
                    rc = EXIT_FAILED
                    answers["samples_failed"] += 1
                    if not f["pages"]:
                        sample_rows.append([n, london_dt(at), "failed", "-", "-", "-", "-", "-", "-"])
                        if n == 1:
                            optional_checks()
                        if n < args.repeat:
                            sleep_between(args.interval, findings, retry_after_s)
                            retry_after_s = None
                        continue
                    findings.add("Analysing the pages that did arrive.")

                a = analyse(f["pages"], box, roads, index)
                report_analysis(findings, a, index=index, roads=roads, label=f"Sample {n}", stem=stem)
                write_records(outdir, stem, a, box)
                merge_distinct(merged_distinct, a["distinct"])
                answers["samples_done"] += 1
                answers["in_box_total"] += a["n_in_box"]
                answers["on_roads_total"] += a["n_on_roads"]
                answers["breakdown_causes"].update(a["breakdown_causes"])
                answers["vehicle_obstruction_records"] += a["vehicle_obstruction_records"]
                answers["point_records"] += a["points"]["records_with_point"]
                answers["other_record_types"].update(a["other_record_types"])
                answers["poslist_disagreements"] += len(a["poslist"]["disagreements"])
                answers["poslist_line_strings"] += a["poslist"]["line_strings"]

                sample = {
                    "run_started": common.iso(started), "at": common.iso(at), "at_london": london(at),
                    "window_hours": args.window_hours, "pages": len(f["pages"]),
                    "n_records": a["n_records"], "n_in_box": a["n_in_box"], "n_on_roads": a["n_on_roads"],
                    "source_ids": a["source_ids"], "in_box_ids": a["in_box_ids"],
                }
                comparison = update_state(state, sample)
                write_json(state_path, state)
                findings.section(f"Sample {n}: change since the previous sample")
                if comparison is None:
                    findings.add("No previous sample in state.json; nothing to compare yet.")
                    cmp_cells = ["-", "-", "-"]
                else:
                    prev_dt = nh.parse_iso(comparison.get("previous_at"))
                    if prev_dt is not None:
                        findings.kv("Previous sample (London)",
                                    f"{london_dt(prev_dt)} ({age_text((at - prev_dt).total_seconds())} "
                                    "before this sample)")
                    else:
                        findings.kv("Previous sample (London)", comparison.get("previous_at_london"))
                    for field, label in (("source_ids", "All records"), ("in_box_ids", "In-box records")):
                        c = comparison[field]
                        findings.kv(f"{label}: persisted / vanished / new",
                                    f"{c['persisted']} / {c['vanished']} / {c['new']}")
                    c = comparison["in_box_ids"]
                    if c["vanished_ids"]:
                        findings.kv("In-box ids that vanished", c["vanished_ids"])
                    if c["new_ids"]:
                        findings.kv("In-box ids that are new", c["new_ids"])
                    call = comparison["source_ids"]
                    cmp_cells = [call["persisted"], call["vanished"], call["new"]]
                sample_rows.append([n, london_dt(at), len(f["pages"]), a["n_records"], a["n_in_box"],
                                    a["n_on_roads"]] + cmp_cells)

                if n == 1:
                    optional_checks()
                if n < args.repeat:
                    sleep_between(args.interval, findings, retry_after_s)
                    retry_after_s = None
    except KeyboardInterrupt:
        findings.add("\nStopped by the user (Ctrl-C). Writing what was found so far.")
        rc = EXIT_FAILED
    except SystemExit as exc:
        # RequestBudget refuses to go past the cap by raising SystemExit.
        findings.add(f"\n{exc}")
        rc = EXIT_FAILED

    if key_rejected:
        findings.section("Result")
        findings.add("The key was rejected, so nothing else was tried. Exit code 4.")
        findings.section("Requests")
        findings.kv("Requests sent", budget.totals())
        findings.write(outdir)
        return EXIT_KEY_REJECTED

    write_answers(findings, answers, merged_distinct, sample_rows, budget, limiter, started, index, roads)
    findings.write(outdir)
    return rc


def sleep_between(interval: float, findings: Findings, retry_after_s: Optional[float] = None) -> None:
    """Wait --interval seconds, or longer when a 429 asked for more."""
    wait = max(float(interval), float(retry_after_s or 0.0))
    if wait <= 0:
        return
    why = (f" (the API asked us to wait {retry_after_s:.0f} s after the 429)"
           if retry_after_s is not None and retry_after_s > interval else "")
    due = now_utc() + timedelta(seconds=wait)
    findings.add(f"\nNext sample at {london(due)} London (in {wait:.0f} s){why}. Ctrl-C to stop.")
    time.sleep(wait)


def run_optional_checks(args: Any, answers: dict[str, Any], client: Any, headers: dict,
                        budget: RequestBudget, limiter: RateLimiter, findings: Findings, outdir: Path,
                        box: Any) -> tuple[bool, Optional[float]]:
    """--no-dates, --xml-check, --test-30-days: one call each, each reported.
    After an HTTP 429 the remaining checks are not sent. Returns
    (a 429 was seen, the wait it asked for)."""
    rate_limited = False
    retry_after_s: Optional[float] = None

    def limited_by(f: dict[str, Any]) -> bool:
        nonlocal rate_limited, retry_after_s
        if f["rate_limited"]:
            rate_limited = True
            if f["retry_after_s"] is not None:
                retry_after_s = max(retry_after_s or 0.0, f["retry_after_s"])
        return rate_limited

    def skipped(flag: str) -> dict[str, Any]:
        findings.add(f"Skipping {flag} after the 429; run again later.")
        return {"status": None, "error": "an HTTP 429 came earlier in this run"}

    if args.no_dates:
        findings.section("Check --no-dates: closureType=unplanned with no start/end")
        findings.add("The portal says the window then defaults to now .. end of today.")
        f = fetch_pages(client, nh.NH_BASE, {"closureType": "unplanned"}, headers, budget=budget,
                        limiter=limiter, findings=findings, outdir=outdir, stem="no_dates",
                        max_pages=args.max_pages)
        limited_by(f)
        res: dict[str, Any] = {"status": f["last_status"], "error": f["error"] or f["stopped"],
                               "pages": len(f["pages"]), "x_next_seen": f["x_next_seen"]}
        if f["pages"]:
            a = analyse(f["pages"], box, [], None)
            res.update({"situations": a["n_situations"], "records": a["n_records"],
                        "earliest": london_dt(a["earliest_start"]), "latest": london_dt(a["latest_start"])})
            findings.kv("Situations / records without dates", f"{a['n_situations']} / {a['n_records']}")
            findings.kv("Start times span (London)", f"{res['earliest']} .. {res['latest']}")
            findings.add("Compare with the sample above (a "
                         f"{args.window_hours:g} h window): fewer records here means the default window "
                         "really is now .. end of today and the service must always send dates.")
        answers["no_dates"] = res

    if args.xml_check and rate_limited:
        answers["xml_check"] = skipped("--xml-check")
    elif args.xml_check:
        findings.section("Check --xml-check: one call WITHOUT X-Response-MediaType")
        findings.add("The portal says the default answer is XML; this confirms it. Only the first 2 KB is saved.")
        no_media = {k: v for k, v in headers.items() if k.lower() != nh.HEADER_MEDIA.lower()}
        params = nh.build_params(now_utc(), args.window_hours)
        f = fetch_pages(client, nh.NH_BASE, params, no_media, budget=budget, limiter=limiter,
                        findings=findings, outdir=outdir, stem="xml_check", follow=False,
                        max_pages=1, max_body_bytes=XML_CHECK_BODY_BYTES, expect_json=False)
        limited_by(f)
        kind = f["body_kinds"][0] if f["body_kinds"] else None
        ct = f["content_types"][0] if f["content_types"] else None
        findings.kv("Content-Type without the header", ct)
        findings.kv("Body looks like", kind)
        if kind == "xml":
            findings.add("Confirmed: without X-Response-MediaType the feed answers in XML. "
                         "The service must always send X-Response-MediaType: application/json.")
        elif kind == "json":
            findings.add("Not as documented: the feed answered JSON even without the header. "
                         "Sending it stays harmless.")
        answers["xml_check"] = {"status": f["last_status"], "content_type": ct, "body_kind": kind,
                                "error": f["error"] or f["stopped"]}

    if args.test_30_days and rate_limited:
        answers["test_30_days"] = skipped("--test-30-days")
    elif args.test_30_days:
        findings.section(f"Check --test-30-days: one call with a {LONG_WINDOW_DAYS}-day window")
        findings.add("A third party reported HTTP 500 for windows over 30 days (unconfirmed).")
        params = nh.build_params(now_utc(), LONG_WINDOW_DAYS * 24)
        findings.kv("Query (31-day window)", params)
        f = fetch_pages(client, nh.NH_BASE, params, headers, budget=budget, limiter=limiter,
                        findings=findings, outdir=outdir, stem="test_30_days", follow=False, max_pages=1)
        limited_by(f)
        status = f["last_status"]
        res = {"status": status, "error": f["error"] or f["stopped"], "x_next_seen": f["x_next_seen"]}
        if status is None:
            findings.add(f"Not sent: {f['stopped'] or f['error']}")
        elif status == 500:
            findings.add("Confirmed: a 31-day window returns HTTP 500 (body printed above). "
                         "Keep windows short.")
        elif status == 200:
            n_sit = f["situations_per_page"][0] if f["situations_per_page"] else 0
            res["situations_first_page"] = n_sit
            findings.kv("Situations on the first page", n_sit)
            findings.kv("More pages (x-next present)", f["x_next_seen"])
            findings.add("Not confirmed: a 31-day window answered HTTP 200.")
        elif status is not None:
            findings.add(f"A 31-day window answered HTTP {status}, not 500 (message above).")
        answers["test_30_days"] = res
    return rate_limited, retry_after_s


def write_answers(findings: Findings, answers: dict[str, Any], merged: dict[str, Counter],
                  sample_rows: list[list[Any]], budget: RequestBudget, limiter: RateLimiter,
                  started: datetime, index: Optional[RoadIndex], roads: list[str]) -> None:
    findings.section("Samples")
    findings.table(sample_rows, header=["sample", "time (London)", "pages", "records", "in box", "on roads",
                                        "persisted", "vanished", "new"])

    findings.section("Answers")
    findings.kv("Header name confirmed (a call with Ocp-Apim-Subscription-Key succeeded)", answers["header_confirmed"])
    xc = answers["xml_check"]
    if xc is None:
        findings.kv("Default media type (--xml-check)", "not tested (pass --xml-check)")
    elif xc.get("body_kind"):
        findings.kv("Default media type (--xml-check)", f"{xc['body_kind']} (Content-Type {xc['content_type']})")
    elif xc.get("status") is None:
        findings.kv("Default media type (--xml-check)", f"not sent: {xc.get('error')}")
    else:
        findings.kv("Default media type (--xml-check)", f"unclear: HTTP {xc.get('status')}; {xc.get('error') or 'no answer body'}")
    nd = answers["no_dates"]
    if nd is None:
        findings.kv("Window default behaviour (--no-dates)", "not tested (pass --no-dates)")
    elif nd.get("records") is not None:
        findings.kv("Window default behaviour (--no-dates)",
                    f"{nd['situations']} situations / {nd['records']} records without dates, "
                    f"start times {nd['earliest']} .. {nd['latest']}; compare the sample table above")
    elif nd.get("status") is None:
        findings.kv("Window default behaviour (--no-dates)", f"not sent: {nd.get('error')}")
    else:
        findings.kv("Window default behaviour (--no-dates)",
                    f"HTTP {nd.get('status')}; {nd.get('error') or 'no records could be read'}")
    findings.kv("Pagination observed (most pages in one sample)", answers["pages_max"])
    findings.kv("x-next header seen", answers["x_next_seen"])
    findings.kv("Distinct causeType values (all samples)", dict(merged.get("cause_type", Counter())))
    findings.kv("Distinct detailedCauseType values (all samples)", dict(merged.get("detailed_cause", Counter())))
    findings.kv("Distinct management types (all samples)", dict(merged.get("management_type", Counter())))
    findings.kv("Distinct sourceIdentification values (all samples)", dict(merged.get("source_identification", Counter())))
    findings.kv("Breakdown-specific causes appeared", bool(answers["breakdown_causes"]))
    findings.kv("Breakdown-specific cause counts", dict(answers["breakdown_causes"]) or "none")
    findings.kv("Records with causeType vehicleObstruction (all samples)", answers["vehicle_obstruction_records"])
    findings.kv("Feed carried point locations (locPointLocation)", answers["point_records"] > 0)
    findings.kv("Point-location records (all samples)", answers["point_records"])
    findings.kv("Record type keys other than the documented one", dict(answers["other_record_types"]) or "none")
    findings.kv("posList order matched the parser (lat lon)",
                answers["poslist_disagreements"] == 0 if answers["poslist_line_strings"] else "no line strings seen")
    findings.kv("Records in the box (sum over samples)", answers["in_box_total"])
    findings.kv("Records in the box on " + "/".join(roads) + " (sum over samples)",
                answers["on_roads_total"] if index else "n/a (no roads.json)")
    findings.kv("Samples completed / failed", f"{answers['samples_done']} / {answers['samples_failed']}")
    t30 = answers["test_30_days"]
    if t30 is None:
        findings.kv("31-day window (--test-30-days)", "not tested (pass --test-30-days)")
    elif t30.get("status") is None:
        findings.kv("31-day window (--test-30-days)", f"not sent: {t30.get('error')}")
    else:
        findings.kv("31-day window (--test-30-days)", f"HTTP {t30.get('status')}")

    findings.section("Requests")
    elapsed = (now_utc() - started).total_seconds()
    findings.kv("Requests sent", budget.totals())
    findings.kv("Run length (s)", round(elapsed, 1))
    findings.kv("Busiest minute (calls)", limiter.max_in_any_window())
    findings.kv("Rate cap (calls a minute)", limiter.max)
    findings.kv("Time spent waiting for the rate cap (s)", round(limiter.total_slept, 1))
    findings.add("The feed carries no personal data; the saved pages are complete. The key is never saved.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def config_number(section: dict, name: str, default: float, notes: list[str]) -> float:
    """A number from config.yaml's nh: section, or the default (with a note
    for the findings) when it is missing or not a number."""
    value = section.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        notes.append(f"config.yaml nh.{name} is not a number ({value!r}); using {default:g}")
        return default


def main(argv: list[str] | None = None) -> int:
    cfg, outdir, parser = common.setup(
        "nh",
        "Fetch National Highways unplanned closures for the last few hours (following pagination, "
        "at most 5 calls a minute), parse them, and report which cause values, shapes and roads the "
        "live feed carries for our box. --parse-fixture works offline without a key.",
        argv=argv, max_requests_default=DEFAULT_MAX_REQUESTS,
    )
    cfg_nh = cfg.get("nh") or {}
    notes: list[str] = []
    default_window = float(config_number(cfg_nh, "window_hours", DEFAULT_WINDOW_HOURS, notes))
    if default_window <= 0:
        notes.append(f"config.yaml nh.window_hours is {default_window:g}; using {DEFAULT_WINDOW_HOURS:g}")
        default_window = DEFAULT_WINDOW_HOURS
    default_rate = int(config_number(cfg_nh, "max_calls_per_min", DEFAULT_MAX_CALLS_PER_MIN, notes))
    if not 1 <= default_rate <= NH_KEY_LIMIT_PER_MIN:
        clamped = min(max(default_rate, 1), NH_KEY_LIMIT_PER_MIN)
        notes.append(f"config.yaml nh.max_calls_per_min is {default_rate}; the key allows "
                     f"{NH_KEY_LIMIT_PER_MIN}, so {clamped} is used")
        default_rate = clamped
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="number of samples to take (default 1)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, metavar="SECONDS",
                        help=f"seconds between samples with --repeat (default {DEFAULT_INTERVAL_S})")
    parser.add_argument("--window-hours", type=float, default=default_window, metavar="H",
                        help=f"startDateTime = now - H hours, endDateTime = now (default {default_window:g})")
    parser.add_argument("--no-dates", action="store_true",
                        help="also make one call with no start/end to show the default window")
    parser.add_argument("--xml-check", action="store_true",
                        help="also make one call without X-Response-MediaType to confirm the default is XML (saves 2 KB)")
    parser.add_argument("--test-30-days", action="store_true",
                        help=f"also make one call with a {LONG_WINDOW_DAYS}-day window to check the reported HTTP 500")
    parser.add_argument("--max-calls-per-min", type=int, default=default_rate, metavar="N",
                        help=f"never send more than N calls in any minute, pagination included (default {default_rate}; "
                             f"the key allows {NH_KEY_LIMIT_PER_MIN})")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, metavar="N",
                        help=f"stop following x-next after N pages per call (default {DEFAULT_MAX_PAGES})")
    parser.add_argument("--state", default=str(common.REPO_ROOT / "fixtures" / "nh" / "live" / "state.json"),
                        metavar="FILE", help="where the ids seen per sample are kept (default fixtures/nh/live/state.json)")
    parser.add_argument("--parse-fixture", nargs="+", default=None, metavar="PATH",
                        help="offline: parse this saved response (or several, as pages) and print the "
                             "record table and distinct values; no key, no request")
    args = parser.parse_args(argv)

    problems = []
    if args.repeat < 1:
        problems.append("--repeat must be at least 1")
    if args.interval < 0:
        problems.append("--interval must not be negative")
    if args.window_hours <= 0:
        problems.append("--window-hours must be more than 0")
    if not 1 <= args.max_calls_per_min <= NH_KEY_LIMIT_PER_MIN:
        problems.append(f"--max-calls-per-min must be between 1 and {NH_KEY_LIMIT_PER_MIN} (the key's own limit)")
    if args.max_pages < 1:
        problems.append("--max-pages must be at least 1")
    if args.parse_fixture:
        for p in args.parse_fixture:
            if not Path(p).is_file():
                problems.append(f"--parse-fixture: file not found: {p}")
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        common.remove_if_empty(outdir)
        return EXIT_FAILED

    if args.parse_fixture:
        return run_offline(args, cfg, outdir)

    key = secret("NH_API_KEY")
    if not key:
        missing_key("NH_API_KEY", outdir)
    return run_live(args, cfg, outdir, key, notes=notes)


if __name__ == "__main__":
    sys.exit(main())
