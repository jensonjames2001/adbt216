# Phase 0 findings — 30 September 2026

Status: **probe tooling built and tested; live probes not yet run.** No API keys were
available where this was built, and the Waze endpoint refuses datacentre addresses, so the
live part of Phase 0 has to run with the owner's keys, ideally on the office machine that
will run the service. Everything below that did not need a key was checked today against
the providers' own documentation and, where possible, live requests.

## 1. What differed from the brief, or was unconfirmed and is now confirmed

| # | Brief said | Found today | Source |
|---|---|---|---|
| 1 | NH header "has not been confirmed" | Confirmed: `Ocp-Apim-Subscription-Key` header (a `subscription-key` query parameter is the alternative) | portal API definition |
| 2 | (not mentioned) | NH answers in **XML by default**. The service must send `X-Response-MediaType: application/json`. | portal API definition |
| 3 | "paginated" | Cursor pagination: the response header `x-next` holds the next page's URL (`PageCursor=…`); the first call must have no cursor; follow until absent. The probe only follows an `x-next` that points back at the NH host. | portal API definition |
| 4 | Without dates the window "is narrow" | Correct: it defaults to now → 23:59:59 today. Portal advice: start = now − 6 h, end = now, `modifiedSinceDateTime` = now − 15 min for changes only | portal description |
| 5 | Over 30 days returns HTTP 500 [reported] | Unconfirmed; the probe has a `--test-30-days` switch | — |
| 6 | Live unplanned records carry only a generic cause [reported] | NH's **own documented "incident" example** carries `causeType: vehicleObstruction`, `vehicleObstructionType: brokenDownVehicle`, a point with latitude/longitude, road name `M62`, a direction, closed lane numbers, source "Incident Management" and the comment "Breakdown [M62A/M60A, Eccles …]". The feed is *designed* to carry breakdowns. Whether the live feed populates them is the main NH question for the live probe. | portal samples |
| 7 | (not mentioned) | NH's own sample contains the typo `souththbound`; the parser reads directions tolerantly. NH's FAQ confirms 10 calls per key per minute and that unplanned closures come from the roadside signs system, so nothing appears where no signs are set. | portal samples, FAQ |
| 8 | TomTom quotas 2,500 / 200,000 [docs] | Confirmed on the pricing page: Traffic Incidents Details "2.5K monthly", Vector Tiles "200K monthly", no card needed | docs.tomtom.com/pricing |
| 9 | TomTom endpoint details [docs] | All confirmed: layer names as in the sample output, `cluster_id`/`cluster_size`, category 14, `ETag`/`If-None-Match`/304, GET max 5 ids, POST max 100, bbox max 10,000 km². Docs last edited September 2026, still on the legacy endpoints, no shutdown date. Tile counts for the box computed and tested: 4 / 9 / 30 at zoom 10 / 11 / 12, as the brief said. | developer.tomtom.com |
| 10 | Waze robots.txt disallows the endpoint | Confirmed: `User-agent: *` → `Disallow: /live-map/api`. From this cloud machine the endpoint returns **HTTP 403** with an HTML "403 Forbidden" page, exactly as the brief warned for datacentre addresses. Untested from broadband. | waze.com/robots.txt, live request |
| 11 | Box drawn from junction positions in memory | Real OpenStreetMap junction data: the box holds **M25 J11–J16** (the website says J12–J16; J11 Chertsey sits inside because the south edge is 51.36), **M4 J1–J7 with 4A and 4B**, **M3 J1–J2**, and the **A316 has no numbered junctions** (only named ones: Apex Corner, Sunbury Cross). The box also holds M40 J1/J1A/J2 and many A-road junctions, ignored because those roads are not in the list. | OpenStreetMap |
| 12 | Sample alert says "J13 (Staines) to J14 (Heathrow T4)" | OpenStreetMap names those junctions "Runnymede" and "Poyle" (M25: J11 Chertsey, J12 Thorpe, J13 Runnymede, J14 Poyle, J15 Thorney, J16 Denham; M4: J1 Chiswick Roundabout, J2 Brentford, J3 Cranford Parkway, J4 Heathrow, J4A Concorde Roundabout, J4B Thorney, J5 Langley Roundabout, J6 Tuns Lane, J7 Huntercombe Spur; M3: J1 Sunbury Cross, J2 Thorpe). The code never invents names, so alerts will show the OSM names unless the owner supplies a name table in `config.yaml` in Phase 1. | OpenStreetMap |
| 13 | Overpass for junctions | overpass-api.de resets connections from here; overpass.openstreetmap.fr works. The fetch script tries both. The data is saved in the repo, so no refetch is needed unless the box changes. | live requests |

Nothing checked today contradicted the TomTom tile budget table or the Telegram plan.

## 2. Still unknown — needs keys or the office machine

These are exactly what the probes answer:

- **TomTom**: whether zoom-10 tiles list every breakdown or hide some in clusters (tiles at 10/11/12 against one Details bbox call, sampled over a day); which y-axis convention lands decoded points on the carriageway (the probe measures both against the OSM road geometry); whether an incident keeps its id between polls; what an exhausted quota looks like (403 vs 429); how many breakdowns the box shows per hour.
- **National Highways**: which cause values appear live; whether point-location records appear; how many records fall in the box and on the four roads; pagination in practice; the 30-day 500.
- **Waze**: whether the endpoint answers from the office broadband at all, what fields it returns, how many alerts (the ~200 cap), how noisy the two car-stopped subtypes are.
- **Telegram**: the chat id (the Telegram probe prints it) and whether the sample alert reads well on a phone.

## 3. What was built

`probes/README.md` has the exact commands. In short:

| Script | Requests per run | Answers |
|---|---|---|
| `probes/probe_tomtom.py` | 46 per sample (43 tiles, 1 ETag re-request, 2 Details); `--repeat 6 --interval 3600 --max-requests 276` samples six hours | zoom completeness, y-axis, id stability, quota response, breakdowns per hour |
| `probes/probe_nh.py` | 1 per sample plus 1 per extra page, never more than 5 a minute | live cause values, point locations, counts in box and on the four roads, pagination |
| `probes/probe_waze.py` | exactly 1 (or N×N with `--grid`, 2 minutes apart) | blocked or not; alert fields, counts, noise |
| `probes/probe_telegram.py` | 2, or 5 with `--send` | bot works, chat id, what an alert and its edits look like |
| `scripts/fetch_junctions.py` | 2 to Overpass, or none with `--from-fixture` | rebuilds `data/junctions.json` and `data/roads.json` when the box changes |

Libraries the service will reuse: tile maths and the TomTom tile decoder (`rgalerts/geo.py`, `rgalerts/sources/tomtom_tiles.py`), the National Highways DATEX II parser (`rgalerts/sources/nh_parse.py`), the road index and junction labeller (`rgalerts/roads.py`, `rgalerts/junctions.py`). 238 tests pass, including parser tests against NH's own sample responses and a decoder test on a synthetic tile.

Every probe prints its request plan first, refuses to exceed a per-run cap, strips keys from every saved URL and header, and drops reporter names and user ids before saving anything. Raw responses go to `fixtures/<source>/live/<timestamp>/` (git-ignored); each run ends with a `findings.md` to send back.

## 4. What I need from the owner

1. **Keys.** Either add `TELEGRAM_BOT_TOKEN`, `TOMTOM_API_KEY` and `NH_API_KEY` to this cloud environment's settings so the next session can run the TomTom and NH probes from here, or run the probes on the office machine and send back the `findings.md` files. Never paste keys into chat.
2. **Chat id.** Message the bot once (or add it to the dispatch group and post something), then run the Telegram probe; it prints the chat id. Put it in `.env` as `TELEGRAM_CHAT_ID` (or in the environment settings). A group id is better than a private chat: everyone on shift sees the alerts and a group id is nobody's personal data.
3. **Waze.** Run the Waze probe from the office broadband. If it prints "blocked from this machine", the direct route is not available and the choice is a paid reseller (backend B) or no Waze.
4. **Box.** Confirm the coverage: M25 J11–J16, M4 J1–J7, M3 J1–J2, A316. To change it, edit `box:` in `config.yaml` and run `scripts/fetch_junctions.py --summary`.
5. **Junction names.** Say whether the OSM names (Runnymede, Poyle, …) are fine or whether alerts should use the signed names (Staines, Heathrow T4, …). If the latter, list them and Phase 1 adds a name table to `config.yaml`.

## 5. Attribution

Junction and road data: Map data © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright).
