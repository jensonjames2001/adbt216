"""Waze probe (optional add-on, no key): does the undocumented live-map
endpoint answer from THIS machine, and what does it return for our box?

Run from the repo root:

    .venv/bin/python probes/probe_waze.py              # exactly one request for the whole box
    .venv/bin/python probes/probe_waze.py --grid 2     # 4 requests, one per cell, 2 min apart (about 6 min)

Exit codes: 0 = answered with JSON, 2 = blocked from this machine (HTTP 403,
429, a redirect, or an HTML/challenge page instead of JSON), 1 = other error.
A 5xx answer is Waze's own outage, not a block: it exits 1 and says so.

Be aware: this endpoint is undocumented, waze.com/robots.txt disallows
/live-map/api for every agent, and it may stop working at any time. The
service must never poll it faster than every 2 minutes, one request at a
time, with the honest User-Agent this probe uses. No proxies, no browser
tricks: if it is blocked, it is blocked, and backend B (a paid reseller) is
the alternative.

What is saved (fixtures/waze/live/<timestamp>/): request meta with headers,
and a CLEANED copy of the alerts. Reporter names, user ids, handles, avatars,
image ids and comments are stripped before anything is written. The count of
thumbs-up (nThumbsUp) is kept: it is a number, not a person.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from probes import common  # noqa: E402
from probes.common import (  # noqa: E402
    EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, Findings, RequestBudget, explain_exception,
    now_utc, redacted_url, save_response, status_line, timed_request, write_json,
)

GEORSS_URL = "https://www.waze.com/live-map/api/georss"
MAX_GRID = 3
# Rule 6: never faster than one request every 2 minutes, even inside a --grid
# run. --grid 2 therefore takes about 6 minutes, --grid 3 about 16.
PAUSE_BETWEEN_CELLS_S = 120.0
CAP_WARN_AT = 190              # the endpoint returns ~200 alerts at most; near that it is truncating
BLOCKED_BODY_BYTES = 500

CAR_STOPPED_SUBTYPES = ("HAZARD_ON_SHOULDER_CAR_STOPPED", "HAZARD_ON_ROAD_CAR_STOPPED")
# Keys dropped (case-insensitive, exact match) from every alert before saving.
PERSONAL_KEYS = {
    "reportby", "reportbyuser", "reportrating", "reporter", "user", "username",
    "avatar", "imageurl", "imageid", "thumbsupby", "comments",
    "users",   # the endpoint's top-level list of reporting users, when present
}
# What the partner (paid reseller) feed documents per alert; used to compare.
EXPECTED_FIELDS = [
    "uuid", "type", "subtype", "location", "pubMillis", "street", "city",
    "roadType", "magvar", "reliability", "confidence", "nThumbsUp", "reportDescription",
]
AGE_BUCKETS_MIN = [(0, 5), (5, 10), (10, 20), (20, 60), (60, None)]


# --------------------------------------------------------------------------
# Pure helpers (tested)
# --------------------------------------------------------------------------

def grid_cells(box, n: int) -> list[dict[str, Any]]:
    """Split the box into n x n cells, north-west first, row by row."""
    if not 1 <= n <= MAX_GRID:
        raise ValueError(f"--grid must be between 1 and {MAX_GRID}")
    d_lat = (box.north - box.south) / n
    d_lon = (box.east - box.west) / n
    cells = []
    for row in range(n):
        top = box.north - row * d_lat
        for col in range(n):
            left = box.west + col * d_lon
            cells.append({
                "name": f"r{row}c{col}" if n > 1 else "box",
                "top": round(top, 5), "bottom": round(top - d_lat, 5),
                "left": round(left, 5), "right": round(left + d_lon, 5),
            })
    return cells


def cell_params(cell: dict[str, Any]) -> dict[str, str]:
    return {
        "top": f"{cell['top']:.4f}", "bottom": f"{cell['bottom']:.4f}",
        "left": f"{cell['left']:.4f}", "right": f"{cell['right']:.4f}",
        "env": "row", "types": "alerts",
    }


def strip_personal(obj: Any) -> Any:
    """Recursively drop personal keys from dicts (lists are walked too)."""
    if isinstance(obj, dict):
        return {k: strip_personal(v) for k, v in obj.items() if str(k).lower() not in PERSONAL_KEYS}
    if isinstance(obj, list):
        return [strip_personal(v) for v in obj]
    return obj


def field_names(obj: Any, prefix: str = "") -> set[str]:
    """Dotted key names of a dict, one level of nesting for location etc."""
    names: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            names.add(f"{prefix}{k}")
            if isinstance(v, dict) and not prefix:
                names |= field_names(v, f"{k}.")
    return names


def grid_minutes(n: int) -> int:
    """How long an n x n run takes: the pauses between its n*n requests."""
    return round((n * n - 1) * PAUSE_BETWEEN_CELLS_S / 60)


def classify(status: int, content: bytes) -> tuple[str, Any]:
    """('ok', data) | ('blocked', data-or-None) | ('error', data-or-None).

    blocked: this machine is refused. HTTP 403 or 429, a redirect (to a
    challenge or sign-in page), or a 200 whose body is not JSON (an HTML
    challenge page). This is the verdict Phase 3 is built on.
    error: something else went wrong. A 5xx is an outage on Waze's side, not
    a block, whatever the body looks like; a 404/410 means the endpoint has
    moved or gone; a 200 with JSON that is not an object is a format change.
    """
    data: Any = None
    if content:
        try:
            data = json.loads(content)
        except ValueError:
            data = None
    if status == 200 and isinstance(data, dict):
        return "ok", data
    if status >= 500 or status in (404, 410):
        return "error", data
    if status in (403, 429) or data is None or 300 <= status < 400:
        return "blocked", data
    return "error", data


def error_message(status: int) -> str:
    """One plain line for an 'error' outcome, by status."""
    if status >= 500:
        return (f"Waze answered with a server error (HTTP {status}); this is not a block. "
                "Try again in a few minutes.")
    if status in (404, 410):
        return (f"Waze answered HTTP {status} (not found): the endpoint has moved or been removed. "
                "This is not a block, but the direct route may be gone for good.")
    return f"unexpected answer (HTTP {status}); saved."


def alert_latlon(alert: dict) -> tuple[float, float] | None:
    loc = alert.get("location")
    if isinstance(loc, dict) and isinstance(loc.get("y"), (int, float)) and isinstance(loc.get("x"), (int, float)):
        return float(loc["y"]), float(loc["x"])
    return None


def is_car_stopped(alert: dict) -> bool:
    return alert.get("subtype") in CAR_STOPPED_SUBTYPES


def age_minutes(alert: dict, now_ms: int) -> float | None:
    pub = alert.get("pubMillis")
    if isinstance(pub, (int, float)):
        return (now_ms - float(pub)) / 60000.0
    return None


def summarise(alerts: list[dict], box, now_ms: int, raw_fields: set[str] | None = None) -> dict[str, Any]:
    """Every number the findings report, from CLEANED alerts."""
    by_type = Counter(str(a.get("type")) for a in alerts)
    by_subtype = Counter(str(a.get("subtype") or "(none)") for a in alerts)
    car = [a for a in alerts if is_car_stopped(a)]
    accidents = [a for a in alerts if a.get("type") == "ACCIDENT"]
    in_box = 0
    for a in car:
        ll = alert_latlon(a)
        if ll and box is not None and box.contains(*ll):
            in_box += 1
    ages = [m for m in (age_minutes(a, now_ms) for a in alerts) if m is not None]
    buckets: dict[str, int] = {}
    for lo, hi in AGE_BUCKETS_MIN:
        label = f"{lo}-{hi} min" if hi is not None else f"{lo}+ min"
        buckets[label] = sum(1 for m in ages if m >= lo and (hi is None or m < hi))
    seen = set()
    for a in alerts:
        seen |= field_names(a)
    raw_fields = raw_fields if raw_fields is not None else seen
    top_level_raw = {f for f in raw_fields if "." not in f}
    return {
        "total": len(alerts),
        "by_type": dict(by_type.most_common()),
        "by_subtype": dict(by_subtype.most_common()),
        "car_stopped": len(car),
        "car_stopped_by_subtype": {s: sum(1 for a in car if a.get("subtype") == s) for s in CAR_STOPPED_SUBTYPES},
        "accidents": len(accidents),
        "car_stopped_road_type": dict(Counter(str(a.get("roadType")) for a in car).most_common()),
        "car_stopped_reliability": dict(Counter(str(a.get("reliability")) for a in car).most_common()),
        "car_stopped_streets": sorted({str(a.get("street") or "(none)") for a in car}),
        "car_stopped_in_box": in_box,
        "fields_seen": sorted(seen),
        "fields_seen_raw": sorted(raw_fields),
        "personal_fields_dropped": sorted(f for f in raw_fields if f.split(".")[-1].lower() in PERSONAL_KEYS),
        "expected_fields_present": [f for f in EXPECTED_FIELDS if f in top_level_raw],
        "expected_fields_missing": [f for f in EXPECTED_FIELDS if f not in top_level_raw],
        "unexpected_fields": sorted(f for f in top_level_raw if f not in EXPECTED_FIELDS
                                    and f.lower() not in PERSONAL_KEYS),
        "age_min": round(min(ages), 1) if ages else None,
        "age_median_min": round(statistics.median(ages), 1) if ages else None,
        "age_max_min": round(max(ages), 1) if ages else None,
        "age_buckets": buckets,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    cfg, outdir, parser = common.setup(
        "waze",
        "One GET to Waze's undocumented live-map endpoint for the config box (no key). "
        "Reports whether this machine is blocked and, if not, what the alerts look like.",
        argv=argv, max_requests_default=MAX_GRID * MAX_GRID,
    )
    parser.add_argument("--grid", type=int, default=1, metavar="N",
                        help=f"split the box into N x N cells, one request every {PAUSE_BETWEEN_CELLS_S:.0f} s "
                             f"(max {MAX_GRID}; --grid 2 takes about {grid_minutes(2)} minutes, "
                             f"--grid {MAX_GRID} about {grid_minutes(MAX_GRID)})")
    args = parser.parse_args(argv)
    if not 1 <= args.grid <= MAX_GRID:
        print(f"--grid must be between 1 and {MAX_GRID}; {args.grid} would send {args.grid * args.grid} requests.",
              file=sys.stderr)
        common.remove_if_empty(outdir)
        return EXIT_FAILED

    box = cfg["box_obj"]
    findings = Findings("Waze probe")
    findings.kv("Run started (UTC)", common.iso(now_utc()))
    findings.add("Reminder: this endpoint is undocumented, disallowed by waze.com/robots.txt for every "
                 "agent, and may stop working at any time. The service must never poll it faster "
                 "than every 2 minutes, one request at a time. This probe sends no retries.")
    findings.kv("Box (W,S,E,N)", f"{box.west}, {box.south}, {box.east}, {box.north}")
    findings.kv("User-Agent", common.USER_AGENT)

    cells = grid_cells(box, args.grid)
    budget = RequestBudget(args.max_requests)
    budget.check(len(cells), f"{args.grid} x {args.grid} cell(s)")
    if len(cells) > 1:
        findings.add(f"{len(cells)} requests, one every {PAUSE_BETWEEN_CELLS_S:.0f} s (never faster than every "
                     f"2 minutes): this run takes about {grid_minutes(args.grid)} minutes. Leave it running.")

    alerts_by_uuid: dict[Any, dict] = {}
    unkeyed: list[dict] = []
    raw_fields: set[str] = set()
    cell_reports: list[dict[str, Any]] = []
    extra_top_level: dict[str, Any] = {}

    with common.http_client(Accept="application/json") as client:
        for i, cell in enumerate(cells):
            if i:
                print(f"Waiting {PAUSE_BETWEEN_CELLS_S:.0f} s before request {i + 1} of {len(cells)} "
                      f"(one request every 2 minutes at most) ...", flush=True)
                time.sleep(PAUSE_BETWEEN_CELLS_S)
            findings.section(f"Request {i + 1} of {len(cells)}: cell {cell['name']}")
            params = cell_params(cell)
            budget.count("waze")
            findings.add(f"GET {redacted_url(str(common.httpx.URL(GEORSS_URL, params=params)))}")
            try:
                response, took = timed_request(client, "GET", GEORSS_URL, params=params)
            except common.httpx.HTTPError as exc:
                findings.add(f"ERROR: request failed: {explain_exception(exc)}")
                findings.write(outdir)
                return EXIT_FAILED
            findings.add(f"  {status_line(response, took)}")
            outcome, data = classify(response.status_code, response.content)

            if outcome == "blocked":
                save_response(outdir, f"cell_{cell['name']}", response, body=True,
                              max_body_bytes=BLOCKED_BODY_BYTES, elapsed_s=took,
                              note="blocked; body kept to the first 500 bytes")
                findings.add(f"Waze is blocked from this machine (HTTP {response.status_code})")
                findings.add("Backend A (direct) is not available here; backend B, a paid reseller of the "
                             "same data, is the alternative, or leave Waze off.")
                if response.status_code in (301, 302, 303, 307, 308):
                    findings.kv("Redirected to", redacted_url(response.headers.get("location", "")))
                snippet = response.content[:120].decode("utf-8", errors="replace").replace("\n", " ")
                findings.kv("Body starts with", snippet)
                findings.kv("Blocked", True)
                findings.section("Requests")
                findings.kv("Requests sent", budget.totals())
                findings.write(outdir)
                return EXIT_BLOCKED

            if outcome == "error":
                save_response(outdir, f"cell_{cell['name']}", response, body=True, elapsed_s=took,
                              max_body_bytes=BLOCKED_BODY_BYTES if data is None else None)
                findings.add(f"ERROR: {error_message(response.status_code)}")
                findings.kv("Blocked", False)
                findings.section("Requests")
                findings.kv("Requests sent", budget.totals())
                findings.write(outdir)
                return EXIT_FAILED

            # 200 with JSON. Never save the raw body: it may name reporters.
            save_response(outdir, f"cell_{cell['name']}", response, body=False, elapsed_s=took,
                          note="body saved separately as alerts.clean.json (personal fields stripped)")
            raw_alerts = data.get("alerts") if isinstance(data.get("alerts"), list) else []
            for a in raw_alerts:
                if isinstance(a, dict):
                    raw_fields |= field_names(a)
            for k, v in data.items():
                if k != "alerts" and str(k).lower() not in PERSONAL_KEYS and not isinstance(v, (list, dict)):
                    extra_top_level[k] = v
            other_lists = {k: len(v) for k, v in data.items() if k != "alerts" and isinstance(v, list)}
            cleaned = [strip_personal(a) for a in raw_alerts if isinstance(a, dict)]
            for a in cleaned:
                if a.get("uuid") is not None:
                    alerts_by_uuid.setdefault(a["uuid"], a)
                else:
                    unkeyed.append(a)
            findings.kv("Alerts in this cell", len(cleaned))
            if other_lists:
                findings.kv("Other lists in the response (not saved)", other_lists)
            if len(cleaned) >= CAP_WARN_AT:
                findings.add(f"WARNING: {len(cleaned)} alerts is at the ~200 cap, so this cell is probably "
                             f"truncated. Try --grid {min(args.grid + 1, MAX_GRID)}.")
            cell_reports.append({**cell, "alerts": len(cleaned), "status": response.status_code,
                                 "elapsed_s": round(took, 3)})

    alerts = list(alerts_by_uuid.values()) + unkeyed
    now_ms = int(now_utc().timestamp() * 1000)
    s = summarise(alerts, box, now_ms, raw_fields)

    write_json(outdir / "alerts.clean.json", {
        "fetched_at": common.iso(now_utc()),
        "box": {"west": box.west, "south": box.south, "east": box.east, "north": box.north},
        "grid": args.grid,
        "cells": cell_reports,
        "stripped_keys": sorted(PERSONAL_KEYS),
        "response_extra": extra_top_level,
        "alerts": alerts,
    })

    findings.section("Alerts")
    findings.kv("Total distinct alerts", s["total"])
    if any(c["alerts"] >= CAP_WARN_AT for c in cell_reports):
        findings.add("WARNING: at least one request returned close to 200 alerts; the endpoint is "
                     "silently truncating. Suggest --grid 2 (or a smaller box).")
    findings.kv("By type", s["by_type"])
    findings.kv("By subtype", s["by_subtype"])
    findings.kv("Car stopped (both subtypes)", s["car_stopped"])
    findings.kv("Car stopped by subtype", s["car_stopped_by_subtype"])
    findings.kv("ACCIDENT", s["accidents"])
    findings.kv("Car stopped: roadType distribution", s["car_stopped_road_type"])
    findings.kv("Car stopped: reliability distribution", s["car_stopped_reliability"])
    findings.kv("Car stopped: distinct streets", s["car_stopped_streets"])
    findings.kv("Car stopped inside the config box", s["car_stopped_in_box"])
    findings.section("Age (now - pubMillis)")
    findings.kv("Minutes: min / median / max", f"{s['age_min']} / {s['age_median_min']} / {s['age_max_min']}")
    findings.kv("Age buckets", s["age_buckets"])
    findings.section("Fields")
    findings.kv("Field names seen (raw, before cleaning)", s["fields_seen_raw"])
    findings.kv("Personal fields dropped", s["personal_fields_dropped"])
    findings.kv("Partner-feed fields present", s["expected_fields_present"])
    findings.kv("Partner-feed fields missing", s["expected_fields_missing"])
    findings.kv("Fields not in the partner-feed list", s["unexpected_fields"])
    if not s["expected_fields_missing"]:
        findings.add("The response carries every field the partner feed documents, so the service's "
                     "Waze parser can serve both backends from one shape.")
    else:
        findings.add("Some partner-feed fields are missing here; the parser must treat them as optional.")
    findings.kv("Blocked", False)
    findings.section("Requests")
    findings.kv("Requests sent", budget.totals())
    findings.write(outdir)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
