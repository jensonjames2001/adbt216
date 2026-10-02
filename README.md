# rgalerts

Breakdown leads for Recovery Giant (24/7 recovery, Feltham). A small Python
service that watches the M25, M3, M4 and A316 around Heathrow for broken-down
vehicles and sends a Telegram message for each one, with map buttons, then edits
the message as more sources confirm it and again when it clears.

**An alert is a lead, not a job.** Every message says "reported": a traffic
provider or a Waze user said there is a stopped vehicle there. Nobody has booked
anything, and the vehicle may be gone by the time anyone arrives.

## Sources

| Source | Status | Notes |
|---|---|---|
| TomTom Traffic (incident tiles + Incident Details) | official API, free tier | 200,000 tile and 2,500 Details requests a month; the service counts every one and stops at a cap |
| National Highways "Road and Lane Closures" (DATEX II) | official API, free key | unplanned closures; used to corroborate and, if the feed carries them, to alert on lane closures |
| Waze live map | **unofficial add-on** | undocumented endpoint, disallowed by waze.com/robots.txt, refuses datacentre addresses, may stop working at any time; polled at most once every 2 minutes with an honest User-Agent, no tricks. A paid reseller is the fallback |

## Phases

0. **Probes** (now): small scripts that ask each provider what it really returns
   for our box, so the service is built on facts. See `probes/README.md`.
1. Telegram + TomTom live service.
2. National Highways corroboration and lane-closure alerts.
3. Waze add-on (direct or via a reseller), a dry-run day, filter tuning.
4. Hardening and handover.

## Quick start

    python -m venv .venv
    .venv/bin/python -m pip install -e ".[dev]"        # Windows: .venv\Scripts\python -m pip install -e ".[dev]"
                                                       # Windows also: .venv\Scripts\python -m pip install tzdata
    copy .env.example to .env and fill in the keys you have
    .venv/bin/python probes/probe_telegram.py           # prints your chat id (use a group chat if you can)
    .venv/bin/python probes/probe_waze.py               # says whether Waze answers from this machine

The `tzdata` line is Windows only: Windows has no time-zone database of its own,
and the probes need one to show London times. Full details in `probes/README.md`.

Coverage and roads are in `config.yaml`. Keys live only in `.env`, which is
git-ignored and is never printed or saved.

## Running on a Raspberry Pi

A Pi 3B on the office broadband is the intended home for the service. Step-by-step
setup, from writing the SD card to running the probes: `docs/PI_SETUP.md`.

## Junction and road data

`data/junctions.json` (motorway junction numbers and names) and `data/roads.json`
(carriageway geometry) come from OpenStreetMap and are already in the repo. They
give every alert its "J13 (Staines) to J14 (Heathrow T4)" line. Rebuild them only
after changing `box` or `roads` in `config.yaml`:

    .venv/bin/python scripts/fetch_junctions.py --summary                 # 2 requests to Overpass, no key
    .venv/bin/python scripts/fetch_junctions.py --from-fixture --summary  # offline, from fixtures/osm/

`--summary` lists the junctions found on each road, which is the thing to read when
deciding whether the box is the right size. Nothing here is typed in by hand: a
junction with no name in OpenStreetMap is shown as just its number.

## Rules the code follows

- Never invents a road, junction, direction or cause; anything a source did not
  say is shown as "unknown".
- Stores no personal data: reporter names, user ids and handles are stripped
  before anything is written (a private Telegram chat id is a user id, so it is
  shown on the screen only).
- Never prints, logs or saves an API key or bot token.
- Counts requests against each provider's limit and stops rather than overrun.
- Times are stored in UTC and shown in London time.

## Status

Phase 0 tooling is built: the probe scaffolding, the Telegram and Waze probes,
the TomTom tile maths and decoder, the National Highways parser, and the junction
and road data for the box. The live probes still need the owner's keys; none were
available where this was built, and Waze answers HTTP 403 from cloud machines.
No service is running yet.

Tests: `.venv/bin/python -m pytest -q`.

Junction and road data: Map data © OpenStreetMap contributors, ODbL 1.0
(openstreetmap.org/copyright).
