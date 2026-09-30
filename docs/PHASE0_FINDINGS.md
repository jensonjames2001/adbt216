# Phase 0 findings — 30 September 2026

Status: **probe tooling built; live probes not yet run.** No API keys were available in
this environment, and the Waze endpoint refuses datacentre addresses, so the live part of
Phase 0 has to run with the owner's keys, ideally on the office machine that will run the
service. Everything below that did not need a key has been checked today.

## 1. What differed from the brief

| # | Brief said | Found today | Source |
|---|---|---|---|
| 1 | NH header "has not been confirmed" | Confirmed: `Ocp-Apim-Subscription-Key` header (the portal's own API definition; a `subscription-key` query parameter is an alternative) | portal API definition |
| 2 | (not mentioned) | NH answers in **XML by default**. The service must send `X-Response-MediaType: application/json`. | portal API definition |
| 3 | "paginated" | Cursor pagination: the response header `x-next` holds the next page's URL (`PageCursor=…`); first call must have no cursor; follow until absent | portal API definition |
| 4 | Without dates the window "is narrow" | Correct: it defaults to now → 23:59:59 today. Portal advice: start = now − 6 h, end = now, `modifiedSinceDateTime` = now − 15 min for changes | portal description |
| 5 | Over 30 days returns HTTP 500 [reported] | Unconfirmed; the probe has a `--test-30-days` switch | — |
| 6 | Live unplanned records carry only a generic cause [reported] | NH's **own documented "incident" example** carries `causeType: vehicleObstruction`, `vehicleObstructionType: brokenDownVehicle`, a point with latitude/longitude, road name `M62`, a direction, closed lane numbers and the comment "Breakdown [M62A/M60A, Eccles …]". So the feed is *designed* to carry breakdowns. Whether the live feed populates them is the main NH question for the live probe. | portal samples |
| 7 | (not mentioned) | NH's own sample contains the typo `souththbound`; the parser reads directions tolerantly | portal samples |
| 8 | TomTom quotas 2,500 / 200,000 [docs] | Confirmed on the pricing page: Traffic Incidents Details "2.5K monthly", Vector Tiles "200K monthly", no card | docs.tomtom.com/pricing |
| 9 | TomTom endpoint details [docs] | All confirmed: layer names as in the sample output, `cluster_id`/`cluster_size`, category 14, `ETag`/`If-None-Match`/304, GET max 5 ids, POST max 100, bbox max 10,000 km². Docs last edited September 2026, still legacy endpoints, no shutdown date. | developer.tomtom.com |
| 10 | Waze robots.txt disallows the endpoint | Confirmed: `User-agent: *` → `Disallow: /live-map/api`. From this cloud machine the endpoint returns **HTTP 403** with an HTML "403 Forbidden" page, exactly as the brief warned for datacentre addresses. Untested from broadband. | waze.com/robots.txt, live request |
| 11 | Box drawn from junction positions in memory | Real OSM junction data: the box holds **M25 J11–J16** (the website says J12–J16; J11 Chertsey sits inside because the south edge is 51.36), **M4 J1–J7 with 4A and 4B**, **M3 J1–J2**. It also holds M40 J1/J1A/J2 and many A-road junctions, which are ignored because those roads are not in the list. | OpenStreetMap |
| 12 | Overpass for junctions | overpass-api.de resets connections from here; overpass.openstreetmap.fr works. The fetch script tries both. The data is saved so no refetch is needed unless the box changes. | live requests |

Nothing checked today contradicted the TomTom tile budget table or the Telegram plan.

## 2. Still unknown — needs keys or the office machine

These are exactly what the probes answer:

- **TomTom**: whether zoom-10 tiles list every breakdown or hide some in clusters (tiles at 10/11/12 vs one Details bbox call, sampled over a day); which y-axis convention lands decoded points on the carriageway; whether an incident keeps its id between polls; what an exhausted quota looks like (403 vs 429); how many breakdowns the box shows per hour.
- **National Highways**: which cause values appear live; whether point-location records appear; how many records fall in the box and on the four roads; pagination in practice; the 30-day 500.
- **Waze**: whether the endpoint answers from the office broadband at all, what fields it returns, how many alerts (the ~200 cap), how noisy the two car-stopped subtypes are.
- **Telegram**: the chat id (the Telegram probe prints it).

## 3. What the tooling does

See `probes/README.md` for exact commands. In short:

- `probes/probe_tomtom.py` — one run costs about 45 tile requests and 2 Details requests; `--repeat 6 --interval 3600` samples across six hours (about 270 tiles, 12 Details, well inside the monthly free tier). Writes `findings.md` answering each unknown.
- `probes/probe_nh.py` — one run is 1 call plus pages, never more than 5 calls a minute.
- `probes/probe_waze.py` — exactly one request, honest User-Agent, stops on 403/429.
- `probes/probe_telegram.py` — prints the bot name and the chat ids it can see; `--send` posts a sample alert and edits it twice, so the owner sees what an alert and its follow-up edits look like.
- `scripts/fetch_junctions.py` — refreshes `data/junctions.json` and `data/roads.json` from OpenStreetMap when the box changes.

Raw responses go to `fixtures/<source>/live/<timestamp>/` (git-ignored) with keys stripped from every saved URL and header.

## 4. What I need from the owner

1. **Keys.** Either add `TELEGRAM_BOT_TOKEN`, `TOMTOM_API_KEY` and `NH_API_KEY` to this cloud environment's settings so the next session can run the probes from here, or run the probes on the office machine and send back the `findings.md` files. Do not paste keys into chat.
2. **Chat id.** Message the bot once (or add it to the dispatch group), then run the Telegram probe; it prints the chat id. Put it in `.env` as `TELEGRAM_CHAT_ID` or in the environment settings.
3. **Waze.** Run the Waze probe from the office broadband. If it prints "blocked from this machine", backend A is not available and the choice is a paid reseller (backend B) or no Waze.
4. **Box.** Confirm the coverage: M25 J11–J16, M4 J1–J7, M3 J1–J2, A316. To change it, edit `box:` in `config.yaml` and rerun `scripts/fetch_junctions.py --summary`.

## 5. Attribution

Junction and road data: Map data © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright).
