#!/usr/bin/env python
"""Fetch motorway junctions and road geometry from OpenStreetMap (Overpass)
and write data/junctions.json and data/roads.json.

    .venv/bin/python scripts/fetch_junctions.py --summary          # live, from Overpass
    .venv/bin/python scripts/fetch_junctions.py --from-fixture     # offline, from fixtures/osm/

Run it again after changing ``box`` or ``roads`` in config.yaml. The
summary lists which junctions fall inside the box, which is what to read
when deciding whether the box is the right size.

Network: 2 requests to the free, volunteer-run Overpass server (no key, no
quota to count), each tried on the next server in the list if the first
fails, so at most 4. Overpass queries take 10-20 s, so this script alone
uses a 90 s HTTP timeout; everything else in rgalerts keeps the 10 s rule.
No retries beyond that fallback.

--from-fixture reads the saved replies in fixtures/osm/, which were fetched
for one particular box and road list (FIXTURE_BOX, FIXTURE_ROADS below); if
config.yaml asks for anything else the script stops and says so rather
than writing data that does not match the config.

Every junction number and name is copied from OSM tags. Nothing is invented.
Map data (c) OpenStreetMap contributors, ODbL 1.0 (openstreetmap.org/copyright).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from rgalerts.config import load_config  # noqa: E402
from rgalerts.junctions import is_motorway, junction_ref_key, normalise_ref  # noqa: E402

ATTRIBUTION = "Map data © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
USER_AGENT = "rgalerts/0.1 (+https://recoverygiantuk.com)"
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]
TIMEOUT_S = 90.0   # deliberately above the 10 s rule: Overpass answers in 10-20 s
FIXTURE_JUNCTIONS = REPO_ROOT / "fixtures" / "osm" / "overpass_junctions_and_ways.json"
FIXTURE_ROADS_GEOM = REPO_ROOT / "fixtures" / "osm" / "overpass_roads_geom.json"
# What the saved replies in fixtures/osm/ were fetched with. An Overpass reply
# carries no record of its query, so this is written down here; update it
# whenever the fixture files are re-saved.
FIXTURE_BOX = {"west": -0.70, "south": 51.36, "east": -0.25, "north": 51.60}
FIXTURE_ROADS = ["M25", "M3", "M4", "A316"]
MAINLINE = "^(motorway|trunk|primary)$"


# ---------------------------------------------------------------------------
# Overpass queries
# ---------------------------------------------------------------------------

def bbox_clause(box) -> str:
    """Overpass wants (south,west,north,east)."""
    return f"({box.south},{box.west},{box.north},{box.east})"


def junction_query(box) -> str:
    return (
        f"[out:json][timeout:{int(TIMEOUT_S)}];"
        f'node["highway"="motorway_junction"]{bbox_clause(box)}->.j;'
        f'way(bn.j)["highway"~"{MAINLINE}"]->.w;'
        ".j out body;.w out body;"
    )


def roads_query(box, roads: list[str]) -> str:
    refs = "|".join(re.escape(r) for r in roads)
    return (
        f"[out:json][timeout:{int(TIMEOUT_S)}];"
        f'way["highway"~"{MAINLINE}"]["ref"~"^({refs})$",i]{bbox_clause(box)};'
        "out tags geom;"
    )


def overpass(query: str, endpoints: list[str]) -> tuple[dict, str]:
    """POST the query to each endpoint in turn; return (json, endpoint used)."""
    errors: list[str] = []
    for url in endpoints:
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(TIMEOUT_S), headers={"User-Agent": USER_AGENT}) as client:
                r = client.post(url, data={"data": query})
            if r.status_code != 200:
                errors.append(f"{url}: HTTP {r.status_code} {r.text[:200].strip()}")
                print(f"  {url} answered HTTP {r.status_code}; trying the next server", flush=True)
                continue
            data = r.json()
            if "elements" not in data:
                errors.append(f"{url}: no 'elements' in the reply")
                continue
            if data.get("remark"):
                print(f"  note from {url}: {data['remark']}", flush=True)
            print(f"  fetched {len(data['elements'])} elements from {url} in {time.monotonic() - t0:.1f} s", flush=True)
            return data, url
        except httpx.HTTPError as e:
            errors.append(f"{url}: {type(e).__name__}: {e}")
            print(f"  {url} failed ({type(e).__name__}); trying the next server", flush=True)
        except ValueError as e:
            errors.append(f"{url}: bad JSON: {e}")
    raise SystemExit("Could not reach any Overpass server:\n  " + "\n  ".join(errors)
                     + "\nCheck the internet connection and try again in a few minutes, "
                       "or run with --from-fixture to use the saved copy.")


# ---------------------------------------------------------------------------
# Build the data files
# ---------------------------------------------------------------------------

def parse_oneway(tags: dict) -> tuple[bool | None, bool]:
    """(oneway, reversed). OSM oneway=-1 means travel runs against node order."""
    v = str(tags.get("oneway", "")).strip().lower()
    if v in ("yes", "true", "1"):
        return True, False
    if v in ("no", "false", "0"):
        return False, False
    if v == "-1":
        return True, True
    return None, False


def parse_lanes(tags: dict) -> int | None:
    v = str(tags.get("lanes", "")).strip()
    return int(v) if v.isdigit() else None


def fixture_problems(raw_j: dict, raw_r: dict, box, roads: list[str]) -> list[str]:
    """Ways the saved Overpass replies do not match config.yaml (empty = fine).

    The saved copy is only right for FIXTURE_BOX and FIXTURE_ROADS. Three
    checks: the config box equals the fixture box, every saved junction node
    lies inside the config box (a safety net if the fixture files were
    re-saved), and every configured road has ways in the saved copy.
    """
    problems: list[str] = []
    if any(abs(float(getattr(box, k)) - v) > 1e-6 for k, v in FIXTURE_BOX.items()):
        problems.append(f"the box is west {box.west}, south {box.south}, east {box.east}, north {box.north}")
    nodes = [e for e in raw_j.get("elements") or [] if e.get("type") == "node"]
    outside = sum(1 for n in nodes if not box.contains(float(n["lat"]), float(n["lon"])))
    if outside:
        problems.append(f"{outside} of the {len(nodes)} saved junction nodes fall outside that box")
    have = {str((w.get("tags") or {}).get("ref") or "").strip().upper()
            for w in raw_r.get("elements") or [] if w.get("type") == "way"}
    missing = [r for r in roads if r not in have]
    if missing:
        problems.append("the saved copy has no ways for " + ", ".join(missing))
    return problems


def build_junctions(raw: dict, roads: list[str]) -> list[dict]:
    els = raw.get("elements") or []
    nodes = {e["id"]: e for e in els if e.get("type") == "node"}
    parents: dict[int, list[str]] = {nid: [] for nid in nodes}
    for w in els:
        if w.get("type") != "way":
            continue
        ref = str((w.get("tags") or {}).get("ref") or "").strip().upper()
        if not ref:
            continue
        for nid in w.get("nodes") or []:
            if nid in parents and ref not in parents[nid]:
                parents[nid].append(ref)
    out = []
    for nid, n in nodes.items():
        tags = n.get("tags") or {}
        prefs = parents[nid]
        road = next((r for r in prefs if r in roads), prefs[0] if prefs else None)
        out.append({
            "osm_id": int(nid),
            "lat": float(n["lat"]),
            "lon": float(n["lon"]),
            "ref": normalise_ref(tags.get("ref")),
            "name": (tags.get("name") or "").strip() or None,
            "road": road,
            "roads": prefs,
        })
    out.sort(key=lambda j: (j["road"] is None, j["road"] or "", junction_ref_key(j["ref"]),
                            j["name"] or "", j["osm_id"]))
    return out


def build_roads(raw: dict, roads: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {r: [] for r in roads}
    for w in raw.get("elements") or []:
        if w.get("type") != "way":
            continue
        tags = w.get("tags") or {}
        ref = str(tags.get("ref") or "").strip().upper()
        if ref not in out:
            continue
        coords = [[round(float(g["lon"]), 7), round(float(g["lat"]), 7)]
                  for g in (w.get("geometry") or []) if g]
        if len(coords) < 2:
            continue
        oneway, reversed_ = parse_oneway(tags)
        if reversed_:
            coords.reverse()   # so node order == direction of travel
        out[ref].append({
            "osm_id": int(w["id"]),
            "highway": tags.get("highway"),
            "oneway": oneway,
            "name": tags.get("name"),
            "lanes": parse_lanes(tags),
            "length_m": round(polyline_length_m(coords), 1),
            "coords": coords,
        })
    for ways in out.values():
        ways.sort(key=lambda w: w["osm_id"])
    return out


def polyline_length_m(coords: list[list[float]]) -> float:
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        h = (math.sin((p2 - p1) / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
        total += 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))
    return total


# ---------------------------------------------------------------------------
# Summary for the owner
# ---------------------------------------------------------------------------

def print_summary(junctions: list[dict], roads_geo: dict[str, list[dict]], roads: list[str], box) -> None:
    print()
    print(f"Box: west {box.west}, south {box.south}, east {box.east}, north {box.north}")
    for road in roads:
        numbered: dict[str, str | None] = {}
        unnumbered: list[str] = []
        for j in junctions:
            if road not in (j["roads"] or ([j["road"]] if j["road"] else [])):
                continue
            # same rule as rgalerts.junctions: a junction number is shown for
            # motorways only (on an A-road the OSM ref is the motorway's number)
            if j["ref"] and is_motorway(road):
                numbered.setdefault(j["ref"], None)
                if j["name"] and not numbered[j["ref"]]:
                    numbered[j["ref"]] = j["name"]
            elif j["name"] and j["name"] not in unnumbered:
                unnumbered.append(j["name"])
        refs = sorted(numbered, key=junction_ref_key)
        ways = roads_geo.get(road, [])
        km = sum(w["length_m"] for w in ways) / 1000.0
        print(f"\n{road}: {len(refs)} numbered junction{'s' if len(refs) != 1 else ''} in the box, "
              f"{len(ways)} way{'s' if len(ways) != 1 else ''}, {km:.1f} km of carriageway")
        if refs:
            print("  " + ", ".join(f"J{r}" + (f" ({numbered[r]})" if numbered[r] else "") for r in refs))
        else:
            print("  (no numbered junctions found in the box)")
        if unnumbered:
            print("  unnumbered (named only): " + ", ".join(sorted(unnumbered)))
    others = sorted({j["road"] for j in junctions if j["road"] and j["road"] not in roads})
    n_other = sum(1 for j in junctions if j["road"] not in roads)
    print(f"\nOther junction nodes kept for a later road change: {n_other} on {', '.join(others) or 'no road'}")
    print(ATTRIBUTION)


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch OSM junctions and road geometry for the configured box.")
    ap.add_argument("--config", default=str(REPO_ROOT / "config.yaml"), help="path to config.yaml")
    ap.add_argument("--endpoint", help="Overpass URL to use instead of the built-in list")
    ap.add_argument("--from-fixture", action="store_true",
                    help="read fixtures/osm/*.json instead of the network (offline)")
    ap.add_argument("--summary", action="store_true", help="print the junctions found per road")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    box = cfg["box_obj"]
    roads = cfg["roads"]
    if not roads:
        print("config.yaml has no roads listed; nothing to do.")
        return 1
    storage = cfg.get("storage") or {}
    cfg_dir = Path(args.config).resolve().parent
    out_junctions = cfg_dir / (storage.get("junctions") or "data/junctions.json")
    out_roads = cfg_dir / (storage.get("roads") or "data/roads.json")

    box_dict = {"west": box.west, "south": box.south, "east": box.east, "north": box.north}
    if args.from_fixture:
        print(f"Reading saved Overpass replies from {FIXTURE_JUNCTIONS.parent} (no network)")
        raw_j = json.loads(FIXTURE_JUNCTIONS.read_text(encoding="utf-8"))
        raw_r = json.loads(FIXTURE_ROADS_GEOM.read_text(encoding="utf-8"))
        problems = fixture_problems(raw_j, raw_r, box, roads)
        if problems:
            fb = FIXTURE_BOX
            raise SystemExit(
                f"The saved copy in {FIXTURE_JUNCTIONS.parent} was fetched for box west {fb['west']}, "
                f"south {fb['south']}, east {fb['east']}, north {fb['north']} and roads "
                f"{', '.join(FIXTURE_ROADS)}; your config.yaml differs ({'; '.join(problems)}), "
                "so run without --from-fixture to fetch fresh data.")
        box_dict = dict(FIXTURE_BOX)   # what the data actually covers
        source = {"mode": "fixture", "endpoint": None,
                  "files": [FIXTURE_JUNCTIONS.name, FIXTURE_ROADS_GEOM.name],
                  "box": dict(FIXTURE_BOX), "roads": list(FIXTURE_ROADS)}
    else:
        endpoints = [args.endpoint] if args.endpoint else ENDPOINTS
        print(f"This makes 2 requests to the free OpenStreetMap Overpass server (no key needed), "
              f"at most {2 * len(endpoints)} if a server is down; each can take 10-20 s.")
        print(f"Asking for junctions in the box and the geometry of {', '.join(roads)} ...")
        raw_j, used = overpass(junction_query(box), endpoints)
        time.sleep(2.0)   # be polite to the volunteer-run server between queries
        raw_r, used2 = overpass(roads_query(box, roads), [used] + [e for e in endpoints if e != used])
        source = {"mode": "overpass", "endpoint": used2}
    source["osm_base"] = (raw_r.get("osm3s") or {}).get("timestamp_osm_base")

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    junctions = build_junctions(raw_j, roads)
    roads_geo = build_roads(raw_r, roads)

    out_junctions.parent.mkdir(parents=True, exist_ok=True)
    with open(out_junctions, "w", encoding="utf-8") as f:
        json.dump({
            "attribution": ATTRIBUTION, "generated_at": generated_at, "source": source,
            "box": box_dict, "roads": roads, "junctions": junctions,
        }, f, ensure_ascii=False, indent=1)
        f.write("\n")
    with open(out_roads, "w", encoding="utf-8") as f:
        json.dump({
            "attribution": ATTRIBUTION, "generated_at": generated_at, "source": source,
            "box": box_dict, "roads": roads_geo,
        }, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")

    n_ways = sum(len(v) for v in roads_geo.values())
    print(f"Wrote {out_junctions} ({len(junctions)} junction nodes) and {out_roads} ({n_ways} ways)")
    missing = [r for r in roads if not roads_geo.get(r)]
    if missing:
        print(f"WARNING: no usable OSM ways found in the box for: {', '.join(missing)}. "
              + ("The saved copy has ways for them but none with two or more points."
                 if args.from_fixture else
                 "Check the road name in config.yaml or widen the box."))
    if args.summary:
        print_summary(junctions, roads_geo, roads, box)
    return 0


if __name__ == "__main__":
    sys.exit(main())
