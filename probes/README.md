# Phase 0 probes

Phase 0 answers the questions we cannot answer from documentation alone, before
any of the live service is written:

- **TomTom**: which tile zoom shows every breakdown in the box (or hides some in
  clusters), which way is "up" inside a decoded tile, whether an incident keeps its
  id from one poll to the next, and how many breakdowns the box shows in an hour.
- **National Highways**: which cause values the live unplanned feed carries,
  whether point locations appear, how many records land on our four roads, and how
  pagination behaves in practice.
- **Waze**: whether the undocumented endpoint answers from the office broadband at
  all, and if so what the alerts look like.
- **Telegram**: that the bot token works, which chat id to send to, and what an
  alert with its two follow-up edits looks like on a phone.

Each probe is one short script. It prints what it is about to do, counts every
request, refuses to go past a per-run cap, saves what came back with every key
stripped, and writes a `findings.md` you can send back as-is.

## Install (once)

You need Python 3.11 or newer (python.org). From the folder that holds this file's
parent (the repo root, the folder with `config.yaml` in it):

    python -m venv .venv

Mac / Linux:

    .venv/bin/python -m pip install -e ".[dev]"

Windows (PowerShell or cmd):

    .venv\Scripts\python -m pip install -e ".[dev]"
    .venv\Scripts\python -m pip install tzdata

That installs httpx, mapbox-vector-tile, PyYAML and pytest. The second Windows line
adds the time-zone database: Windows has none of its own, and without it every
probe stops at once with `No time-zone database found` (the message repeats the
command). Nothing else is needed. Everywhere below, Windows users replace
`.venv/bin/python` with `.venv\Scripts\python`.

## Keys: create .env

Copy `.env.example` to `.env` (same folder) and fill in the values you have:

    TELEGRAM_BOT_TOKEN=   from @BotFather in Telegram
    TELEGRAM_CHAT_ID=     the Telegram probe prints this; fill it in after the first run
    TOMTOM_API_KEY=       developer.tomtom.com, free tier
    NH_API_KEY=           developer.data.nationalhighways.co.uk subscription key

Leave the ones you do not have empty. `.env` is git-ignored and is never printed or
saved by any probe. If a probe needs a value that is missing it stops with
`Set NAME in .env (see .env.example)` and exit code 3.

## Running the probes

Always run from the repo root. Every probe accepts `--help`, `--out DIR` (write
somewhere else), `--config FILE` and `--max-requests N` (the per-run cap; a probe
prints its planned request count first and refuses to exceed the cap). The default
caps are 150 for TomTom, 40 for National Highways, 9 for Waze and 10 for Telegram;
`--help` on any probe lists every flag with its default.

The TomTom and National Highways probes also read `data/roads.json` (which
carriageway a point is on). It is already in the repo; rebuild it only after
changing `box` or `roads` in `config.yaml`, with
`.venv/bin/python scripts/fetch_junctions.py --summary` (see the main README).

### Telegram: `probes/probe_telegram.py`

    .venv/bin/python probes/probe_telegram.py

Send the bot any message first (or add it to the dispatch group and post
something). The probe calls `getMe` and `getUpdates`, prints the bot's name and a
table of the chats it can see, and tells you which id to put in `.env` as
`TELEGRAM_CHAT_ID`. Group ids are negative.

Use a group if you can: everyone on shift sees the alerts, and a group id is
nobody's personal data. A private chat's id is that person's Telegram user id, so
the probe shows private chat ids on the screen only and never writes them to
`findings.md` or any saved file; copy yours from the screen.

If the token is wrong the probe says so in plain words (`TELEGRAM_BOT_TOKEN in
.env is not a valid bot token ...`), and the same for a chat id the bot cannot see
or a group the bot is not in.

    .venv/bin/python probes/probe_telegram.py --send
    .venv/bin/python probes/probe_telegram.py --send --chat-id -1001234567890

`--send` posts the sample alert (first line "This was a test message"), waits 3 s,
edits it to show a second source joining, waits 3 s, edits it again to show it
cleared. Look at the chat while it runs. Cost: 2 requests, or 5 with `--send`.
Telegram is free. If Telegram answers 429 the probe waits the number of seconds it
asks for and retries once.

### TomTom: `probes/probe_tomtom.py`

    .venv/bin/python probes/probe_tomtom.py --plan-only          # prints the request plan, sends nothing, needs no key
    .venv/bin/python probes/probe_tomtom.py                      # one sample: tiles at zoom 10, 11 and 12, two Details calls, one ETag re-request
    .venv/bin/python probes/probe_tomtom.py --repeat 6 --interval 3600   # six samples an hour apart
    .venv/bin/python probes/probe_tomtom.py --keep-sample        # keep every raw tile, not just the interesting ones

Cost per sample: 46 requests. That is 43 tiles (4 + 9 + 30 at zoom 10/11/12 over
the default box), one re-request of the first tile with `If-None-Match` to check
that TomTom answers 304 (it counts as a tile request), and 2 Incident Details
requests (one by bbox, one by ids). Six samples: 264 tile and 12 Details requests,
276 in all; the default per-run cap is 150, so `--repeat 6` needs
`--max-requests 276`. `--plan-only` prints the exact plan for your box without
sending anything. The free tier allows 200,000 tiles and 2,500 Details a month, so
this is well inside it, and the probe still refuses to go past its cap.
It answers: does zoom 10 list every breakdown or hide some in clusters; do decoded
points land on the carriageway (which y-axis convention is right); does an incident
keep its id between polls; what does an exhausted quota look like; how many
breakdowns the box shows per hour.

Other flags, none needed for a normal run: `--zooms 10,11` (fetch fewer zoom
levels, fewer tiles), `--details-limit 0` (skip the two Incident Details calls),
`--no-etag-check` (skip the `If-None-Match` re-request), `--state FILE` (where the
breakdown ids seen per sample are remembered between runs; default
`fixtures/tomtom/live/state.json`).

### National Highways: `probes/probe_nh.py`

    .venv/bin/python probes/probe_nh.py                          # unplanned closures, last 6 hours, follows pagination
    .venv/bin/python probes/probe_nh.py --repeat 3 --interval 600
    .venv/bin/python probes/probe_nh.py --window-hours 24        # a longer window (default 6)
    .venv/bin/python probes/probe_nh.py --no-dates               # also one call without a date window, to see the default
    .venv/bin/python probes/probe_nh.py --xml-check              # also one call without the JSON header, to confirm XML is the default
    .venv/bin/python probes/probe_nh.py --test-30-days           # also one call with a 31-day window, to check the reported 500
    .venv/bin/python probes/probe_nh.py --parse-fixture PATH     # parse a saved response offline, no network, no key

Cost: 1 request per sample plus one per extra page (at most `--max-pages`, default
20), plus one for each of `--no-dates`, `--xml-check` and `--test-30-days`; never
more than 5 requests a minute, pagination included (the key allows 10, and
`--max-calls-per-min` can lower the 5, not raise it past 10). The default per-run
cap is 40. It
answers: which cause values appear live (looking for `vehicleObstruction` /
`brokenDownVehicle`), whether point locations appear, how many records are in the
box and on the M25/M3/M4/A316, and how many pages a 6-hour window needs.
`--state FILE` says where the ids seen per sample are kept between runs (default
`fixtures/nh/live/state.json`). `--parse-fixture` takes several files too, treated
as pages of one response.

### Waze: `probes/probe_waze.py` (optional, no key)

    .venv/bin/python probes/probe_waze.py
    .venv/bin/python probes/probe_waze.py --grid 2               # 4 requests, one per quarter of the box, 2 min apart

Exactly one request for the whole box, or N x N with `--grid` (at most 3). Even
inside a `--grid` run it never sends faster than one request every 2 minutes, so
`--grid 2` takes about 6 minutes and `--grid 3` about 16; leave it running. It
answers the one question that matters: does this machine get an answer? Exit code
2 with `Waze is blocked from this machine (HTTP 403)` means the direct route is not
available from your connection and the choice is a paid reseller (backend B) or no
Waze. Exit code 0 means it answered, and the findings show how many alerts there
were, how many are "car stopped", on which roads, how old, and whether the fields
match the reseller's. If a run reports close to 200 alerts the endpoint is
truncating; try `--grid 2`. A 5xx answer (`Waze answered with a server error`) is
an outage on Waze's side, not a block: exit code 1, try again in a few minutes and
do not report it as blocked. A 404 means the endpoint has moved or gone.

This endpoint is undocumented and disallowed by waze.com/robots.txt. It may stop
working without notice. The probe uses the honest User-Agent
`rgalerts-probe/0.1 (+https://recoverygiantuk.com)`, no retries and no tricks, and
the service will never poll it faster than every 2 minutes.

## Where the output goes

Each run writes to `fixtures/<source>/live/<timestamp>/` (git-ignored), for
example `fixtures/waze/live/20260930T183306Z/`:

- `findings.md` - the readable summary; this is the file to send back
- `summary.json` - the same numbers, machine-readable
- `<step>.meta.json` - status, timing and headers of each request, with the URL and
  every key/token replaced by `***`
- `<step>.body.json` / `.txt` / `.pbf` - what the provider returned, when it is safe
  to keep. Waze alerts are saved only after reporter names, user ids, handles,
  avatars and comments are removed; Telegram updates are saved as update ids,
  chat types, group ids and titles, and text lengths only (no names, and no
  private-chat ids: those are user ids).

## What to send back

1. The `findings.md` from each probe you ran (the whole `live/<timestamp>` folder is
   fine too; there are no keys in it).
2. For Waze: whether it printed **blocked from this machine** or not. That one word
   decides how Phase 3 is built.
3. For Telegram: nothing to send; put the chat id it printed into `.env` (a
   group id if you have one).
4. Anything that looked wrong on the phone in the sample alert (wording, buttons,
   the edits).

Do not paste keys anywhere. If a probe fails, send the exact message it printed.
