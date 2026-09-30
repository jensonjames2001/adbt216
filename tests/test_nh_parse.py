"""Tests for rgalerts.sources.nh_parse against the three NH portal fixtures."""
from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from rgalerts.models import Report
from rgalerts.sources import nh_parse as nh

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "nh"


def load(name: str) -> dict:
    with open(FIXTURES / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def incident() -> dict:
    return load("doc_sample_incident")


@pytest.fixture(scope="module")
def unplanned() -> dict:
    return load("doc_sample_unplanned_single_location")


@pytest.fixture(scope="module")
def planned() -> dict:
    return load("doc_sample_planned_multi_location")


def in_uk(lon: float, lat: float) -> bool:
    return 49.0 <= lat <= 61.0 and -9.0 <= lon <= 3.0


# --------------------------------------------------------------------------
# Incident fixture (the portal's breakdown example)
# --------------------------------------------------------------------------

def test_incident_fixture_is_one_breakdown_report(incident):
    reports = nh.parse_payload(incident)
    assert len(reports) == 1
    r = reports[0]
    assert isinstance(r, Report)
    assert r.source == "nh"
    assert r.source_id == "1742580170-ee8afbb2-efad-4380-954e-a2ec6e9021b2"
    assert r.kind == "breakdown"
    assert r.lat == pytest.approx(53.489254)
    assert r.lon == pytest.approx(-2.3784728)
    assert r.road == "M62"
    assert r.direction == "southbound"          # from the sample's "souththbound" typo
    assert r.position == "in_lane"              # lane 2 (cl2) closed, 1 lane restricted
    assert r.description is not None and r.description.startswith("Breakdown")
    assert r.reported_at == datetime(2025, 3, 20, 11, 3, 12, tzinfo=timezone.utc)
    assert r.reported_at.tzinfo is not None
    assert r.from_place is None and r.to_place is None
    assert r.n_reports is None
    assert r.line == []
    e = r.extra
    assert e["record_type"] == "sitRoadOrCarriagewayOrLaneManagement"
    assert e["management_type"] == "laneClosures"
    assert e["cause_type"] == "vehicleObstruction"
    assert e["detailed_cause"] == "brokenDownVehicle"
    assert e["validity_status"] == "definedByValidityTimeSpec"
    assert e["source_identification"] == "Incident Management"
    assert e["carriageways"] == ["slipRoads"]
    assert e["lanes"] == [{"number": 2, "usage": "cl2", "status": "closed",
                           "direction": "aligned", "carriageway": "slipRoads"}]
    assert e["lanes_restricted"] == 1
    assert e["lanes_operational"] == 1
    assert e["location_description"] == "link road from M62 J10 westbound to M6 J21A northbound"
    assert e["version_time"] == "2025-03-20T11:30:37Z"
    assert e["end_time"] == "2025-03-20T12:24:23Z"
    assert e["probability"] == "certain"
    assert e["shape"] == "point"
    assert e["direction_raw"] == "souththbound"
    assert e["directions_raw"] == ["souththbound"]
    assert e["road_names"] == ["M62"]
    assert e["lines"] == []                     # a point has no polyline
    assert e["start_time"] == "2025-03-20T11:03:12Z"
    assert e["creation_time"] == "2025-03-20T11:28:24Z"
    # extra must be plain JSON (it is stored)
    json.dumps(e)


def test_incident_report_carries_no_personal_data(incident):
    r = nh.parse_payload(incident)[0]
    text = json.dumps(r.to_dict()).lower()
    for banned in ("reportby", "user", "avatar", "email", "phone"):
        assert banned not in text


def test_incident_distinct_values(incident):
    dv = nh.distinct_values(incident)
    assert dv["cause_type"] == Counter({"vehicleObstruction": 1})
    assert dv["detailed_cause"] == Counter({"brokenDownVehicle": 1})
    assert dv["management_type"] == Counter({"laneClosures": 1})
    assert dv["validity_status"] == Counter({"definedByValidityTimeSpec": 1})
    assert dv["record_type"] == Counter({"sitRoadOrCarriagewayOrLaneManagement": 1})
    assert dv["road_name"] == Counter({"M62": 1})
    assert dv["direction"] == Counter({"souththbound": 1})
    assert dv["direction_normalised"] == Counter({"southbound": 1})
    assert dv["carriageway"] == Counter({"slipRoads": 1})
    assert dv["shape"] == Counter({"point": 1})
    assert dv["source_identification"] == Counter({"Incident Management": 1})
    assert dv["kind"] == Counter({"breakdown": 1})
    assert dv["position"] == Counter({"in_lane": 1})
    assert dv["lane_usage"] == Counter({"cl2": 1})
    assert dv["lane_status"] == Counter({"closed": 1})


# --------------------------------------------------------------------------
# Unplanned single-location fixture
# --------------------------------------------------------------------------

def test_unplanned_single_location_fixture(unplanned):
    reports = nh.parse_payload(unplanned)
    assert len(reports) == 1
    r = reports[0]
    # The fixture's cause is roadOrCarriagewayOrLaneManagement / laneClosures,
    # which is neither vehicleObstruction nor accident, so the rule gives lane_closure.
    assert r.extra["cause_type"] == "roadOrCarriagewayOrLaneManagement"
    assert r.extra["detailed_cause"] == "laneClosures"
    assert r.kind == "lane_closure"
    assert nh.classify(next(nh.iter_records(unplanned))[3])[0] == "lane_closure"
    assert r.extra["shape"] == "line"
    assert len(r.line) == 11
    assert len(r.line) >= 2
    assert all(in_uk(lon, lat) for lon, lat in r.line)
    # posList order in the fixture is "lat lon": first pair is 52.777935 -2.118535
    assert r.line[0] == (-2.118535, 52.777935)
    assert r.line[-1] == (-2.114844, 52.771916)
    # representative point is the middle vertex by count (index 5 of 11)
    assert (r.lon, r.lat) == r.line[5]
    assert r.road == "M6"
    assert r.direction == "southbound"          # from "southBound"
    assert r.extra["direction_raw"] == "southBound"
    assert r.position == "in_lane"              # cl1 closed, cl2..cl4 open
    assert r.extra["validity_status"] == "active"
    assert r.extra["management_type"] == "laneClosures"
    assert r.extra["source_identification"] == "Signs and Signals"
    assert r.extra["carriageways"] == ["dualCarriageway"]
    assert [ln["status"] for ln in r.extra["lanes"]] == ["closed", "open", "open", "open"]
    assert r.extra["lanes_restricted"] == 1 and r.extra["lanes_operational"] == 3
    assert r.reported_at == datetime(2025, 3, 21, 6, 1, 57, tzinfo=timezone.utc)
    assert r.source_id == "7-1742580170-ee8afbb2-efad-4380-954e-a2ec6e9021b2-close"
    assert r.description == "laneClosures"
    # one polyline: extra["lines"] carries it as JSON lists, same vertices as line
    assert r.extra["lines"] == [[list(p) for p in r.line]]
    assert r.extra["road_names"] == ["M6"] and r.extra["directions_raw"] == ["southBound"]
    json.dumps(r.extra)


def test_unplanned_distinct_values(unplanned):
    dv = nh.distinct_values(unplanned)
    assert dv["cause_type"] == Counter({"roadOrCarriagewayOrLaneManagement": 1})
    assert dv["detailed_cause"] == Counter({"laneClosures": 1})
    assert dv["validity_status"] == Counter({"active": 1})
    assert dv["road_name"] == Counter({"M6": 1})
    assert dv["direction"] == Counter({"southBound": 1})
    assert dv["carriageway"] == Counter({"dualCarriageway": 1})
    assert dv["shape"] == Counter({"line": 1})
    assert dv["source_identification"] == Counter({"Signs and Signals": 1})
    assert dv["lane_usage"] == Counter({"cl1": 1, "cl2": 1, "cl3": 1, "cl4": 1})
    assert dv["lane_status"] == Counter({"closed": 1, "open": 3})


# --------------------------------------------------------------------------
# Planned multi-location fixture
# --------------------------------------------------------------------------

def test_planned_multi_location_fixture(planned):
    res = nh.parse_payload_detailed(planned)
    assert res.n_records == 1 and res.n_errors == 0 and res.n_skipped_no_coords == 0
    r = res.reports[0]
    assert r.kind == "lane_closure"
    assert r.extra["cause_type"] == "roadMaintenance"
    assert r.extra["detailed_cause"] == "other"        # list-valued roadMaintenanceType
    assert r.extra["shape"] == "group"
    assert r.extra["validity_status"] == "planned"
    assert r.extra["management_type"] == "other"
    assert r.road == "A120"
    assert r.direction == "eastbound"
    assert r.position == "in_lane"
    # overallStartTime (03-14) is earlier than creation (03-19): min() keeps it
    assert r.reported_at == datetime(2025, 3, 14, 8, 0, tzinfo=timezone.utc)
    assert r.extra["start_time"] == "2025-03-14T08:00:00Z"
    assert r.extra["creation_time"] == "2025-03-19T07:16:54Z"
    # line = every vertex of both members (2 + 3); the join between members is
    # not carriageway, which is why extra["lines"] keeps the members apart
    assert len(r.line) == 5
    assert all(in_uk(lon, lat) for lon, lat in r.line)
    assert r.line[0] == (0.525233, 51.868835)
    assert r.line[2] == (0.534082, 51.869825)
    assert [len(m) for m in r.extra["lines"]] == [2, 3]
    assert r.extra["lines"][0] == [[0.525233, 51.868835], [0.525917, 51.869]]
    assert r.extra["lines"][1][0] == [0.534082, 51.869825]
    assert r.line == [tuple(p) for m in r.extra["lines"] for p in m]
    # both members say A120 / eastBound, so the values are kept, and listed
    assert r.extra["road_names"] == ["A120"]
    assert r.extra["directions_raw"] == ["eastBound"]
    json.dumps(r.extra)

    record = next(nh.iter_records(planned))[3]
    loc = nh.parse_location(record)
    group = record["locationReference"]["locLocationGroupByList"]["locationContainedInGroup"]
    assert len(loc["lines"]) == len(group) == 2
    assert [len(l) for l in loc["lines"]] == [2, 3]
    assert loc["points"] == loc["lines"][0] + loc["lines"][1]
    assert loc["road_names"] == ["A120"]
    assert loc["carriageways"] == ["dualCarriageway", "dualCarriageway"]
    assert len(loc["lanes"]) == 2
    assert loc["location_description"].startswith("A120 eastbound between")


def test_planned_distinct_values(planned):
    dv = nh.distinct_values(planned)
    assert dv["cause_type"] == Counter({"roadMaintenance": 1})
    assert dv["detailed_cause"] == Counter({"other": 1})
    assert dv["management_type"] == Counter({"other": 1})
    assert dv["validity_status"] == Counter({"planned": 1})
    assert dv["road_name"] == Counter({"A120": 1})
    assert dv["direction"] == Counter({"eastBound": 1})
    assert dv["carriageway"] == Counter({"dualCarriageway": 2})
    assert dv["shape"] == Counter({"group": 1})
    assert dv["source_identification"] == Counter({"roadworks": 1})
    assert dv["probability"] == Counter({"probable": 1})


def test_parse_payload_accepts_inner_payload_and_list_of_pages(incident, unplanned):
    assert len(nh.parse_payload(incident["D2Payload"])) == 1
    assert len(nh.parse_payload([incident, unplanned])) == 2
    assert nh.parse_payload(None) == []
    assert nh.parse_payload({}) == []


# --------------------------------------------------------------------------
# Request helpers
# --------------------------------------------------------------------------

def test_constants_and_headers():
    assert nh.NH_BASE == "https://api.data.nationalhighways.co.uk/roads/v2.0/closures"
    assert nh.HEADER_KEY == "Ocp-Apim-Subscription-Key"
    h = nh.headers("abc123")
    assert h["Ocp-Apim-Subscription-Key"] == "abc123"
    assert h["X-Response-MediaType"] == "application/json"
    assert h["X-Data-Format"] == "DATEXII"
    with pytest.raises(ValueError):
        nh.headers("")
    with pytest.raises(ValueError):
        nh.headers("   ")


def test_fmt_dt_exact_format():
    dt = datetime(2026, 9, 30, 14, 5, 9, 123456, tzinfo=timezone.utc)
    assert nh.fmt_dt(dt) == "2026-09-30T14:05:09"
    # converted to UTC, no offset suffix, no fraction
    bst = datetime(2026, 9, 30, 15, 5, 9, tzinfo=timezone(timedelta(hours=1)))
    assert nh.fmt_dt(bst) == "2026-09-30T14:05:09"
    with pytest.raises(ValueError):
        nh.fmt_dt(datetime(2026, 9, 30, 14, 5, 9))  # naive is refused


def test_build_params_window_and_format():
    now = datetime(2026, 9, 30, 14, 5, 9, 500000, tzinfo=timezone.utc)
    p = nh.build_params(now)
    assert p == {
        "closureType": "unplanned",
        "startDateTime": "2026-09-30T08:05:09",
        "endDateTime": "2026-09-30T14:05:09",
    }
    p2 = nh.build_params(now, window_hours=6, modified_since_min=15)
    assert p2["modifiedSinceDateTime"] == "2026-09-30T13:50:09"
    assert "pageCursor" not in p2                   # first call never carries a cursor
    p3 = nh.build_params(now, window_hours=1.5)
    assert p3["startDateTime"] == "2026-09-30T12:35:09"
    import re
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
    for v in (p2["startDateTime"], p2["endDateTime"], p2["modifiedSinceDateTime"]):
        assert pat.match(v), v
    with pytest.raises(ValueError):
        nh.build_params(datetime(2026, 9, 30, 14, 5, 9))


def test_next_page_url_case_insensitive_and_missing():
    url = "https://api.data.nationalhighways.co.uk/roads/v2.0/closures?closureType=unplanned&PageCursor=2"
    assert nh.next_page_url({"X-Next": url}) == url
    assert nh.next_page_url({"x-next": url}) == url
    assert nh.next_page_url({"X-NEXT": " " + url + " "}) == url
    assert nh.next_page_url(httpx.Headers({"X-Next": url})) == url
    assert nh.next_page_url([("Content-Type", "application/json"), ("x-next", url)]) == url
    assert nh.next_page_url({"Content-Type": "application/json"}) is None
    assert nh.next_page_url({"x-next": ""}) is None
    assert nh.next_page_url({}) is None
    assert nh.next_page_url(None) is None


def test_redact_url_masks_secret_query_params():
    u = "https://api.data.nationalhighways.co.uk/roads/v2.0/closures?closureType=unplanned&subscription-key=SECRET123&PageCursor=2"
    out = nh.redact_url(u)
    assert "SECRET123" not in out
    assert "closureType=unplanned" in out and "PageCursor=2" in out
    assert "subscription-key=***" in out
    assert nh.redact_url("https://example.org/path") == "https://example.org/path"


def test_redact_url_masks_a_key_in_the_path_when_passed():
    # the portal's own x-next example masks a segment in the PATH, not a query param
    u = "https://api.data.nationalhighways.co.uk/roads/v2.0/closures/SECRETKEY123&PageCursor=abc"
    out = nh.redact_url(u, "SECRETKEY123")
    assert "SECRETKEY123" not in out
    assert out == "https://api.data.nationalhighways.co.uk/roads/v2.0/closures/***&PageCursor=abc"
    # name-based masking alone cannot see it: the caller must pass the key
    assert nh.redact_url(u) == u
    # both passes at once; None / empty secrets are ignored
    u2 = "https://example.org/x/SECRETKEY123?subscription-key=SECRETKEY123&PageCursor=2#SECRETKEY123"
    out2 = nh.redact_url(u2, None, "", "SECRETKEY123")
    assert "SECRETKEY123" not in out2 and "PageCursor=2" in out2


# --------------------------------------------------------------------------
# normalise_direction
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("souththbound", "southbound"),
    ("southBound", "southbound"),
    ("northBound", "northbound"),
    ("eastBound", "eastbound"),
    ("westBound", "westbound"),
    ("NORTHBOUND", "northbound"),
    ("north", "northbound"),
    ("anticlockwise", "anticlockwise"),
    ("anti-clockwise", "anticlockwise"),
    ("counterClockwise", "anticlockwise"),
    ("clockwise", "clockwise"),
    ("bothWays", "both"),
    ("allDirections", "both"),
    ("unknown", None),                # the enum's own unknown: None is the marker
    ("UNKNOWN", None),
    (None, None),
    ("", None),
    ("   ", None),
    ("aligned", None),
    ("opposite", None),
    ("other", None),
    ("innerRing", None),
    ("outerRing", None),
    ("northEastBound", None),
    ("southWestBound", None),
    ("inboundTowardsTown", None),
    ("extendedG", None),
    (42, None),
])
def test_normalise_direction(raw, expected):
    assert nh.normalise_direction(raw) == expected


# --------------------------------------------------------------------------
# posList / coordinates
# --------------------------------------------------------------------------

def test_parse_poslist_lat_lon_order_and_dimension():
    pts = nh.parse_poslist("52.777935 -2.118535 52.777526 -2.118181")
    assert pts == [(-2.118535, 52.777935), (-2.118181, 52.777526)]
    assert nh.poslist_order([(52.777935, -2.118535)]) == "latlon"
    # safety net: a lon-lat list is detected and flipped
    assert nh.parse_poslist("-2.118535 52.777935 -2.118181 52.777526") == pts
    assert nh.poslist_order([(-2.118535, 52.777935)]) == "lonlat"
    # srsDimension 3 drops the height
    assert nh.parse_poslist("52.77 -2.11 10 52.78 -2.12 11", 3) == [(-2.11, 52.77), (-2.12, 52.78)]
    # string dimension, list input, empty
    assert nh.parse_poslist(["52.77", "-2.11"], "2") == [(-2.11, 52.77)]
    assert nh.parse_poslist("") == []
    assert nh.parse_poslist(None) == []
    assert nh.parse_poslist("abc def") == []
    # outside the UK: order is undetectable, documented lat-lon is assumed
    assert nh.poslist_order([(100.0, 100.0)]) is None
    assert nh.parse_poslist("10.0 100.0") == [(100.0, 10.0)]


# --------------------------------------------------------------------------
# classify / position / to_report edge cases on synthetic records
# --------------------------------------------------------------------------

def make_record(cause_type=None, detailed=None, lanes=None, restricted=None, **over):
    """A minimal point record with the given cause and lanes."""
    rec = {
        "idG": "rec-1",
        "situationRecordCreationTime": "2025-03-20T11:28:24Z",
        "situationRecordVersionTime": "2025-03-20T11:30:37Z",
        "locationReference": {
            "locPointLocation": {
                "pointByCoordinates": {"pointCoordinates": {"latitude": 51.45, "longitude": -0.41}},
                "supplementaryPositionalDescription": {
                    "carriageway": [{
                        "carriageway": {"value": "mainCarriageway"},
                        "lane": lanes or [],
                        "carriagewayExtensionG": {"impactOnCarriageway": {"numberOfLanesRestricted": restricted}},
                    }],
                },
            },
        },
    }
    if cause_type is not None:
        rec["cause"] = {"causeType": cause_type}
        if detailed is not None:
            rec["cause"]["detailedCauseType"] = detailed
    rec.update(over)
    return rec


def lane(number, usage, status, ext=True):
    return {
        "laneNumber": number,
        "laneUsage": ({"value": "extendedG", "extendedValueG": usage} if ext else {"value": usage}),
        "laneExtensionG": {"impactOnLanes": {"impactExtensionG": {"lanesStatus": status, "laneImpactDirection": "aligned"}}},
    }


@pytest.mark.parametrize("cause_type, detailed, expected", [
    ("vehicleObstruction", {"vehicleObstructionType": "brokenDownVehicle"}, ("breakdown", "vehicleObstruction", "brokenDownVehicle")),
    ("vehicleObstruction", {"vehicleObstructionType": "damagedVehicle"}, ("breakdown", "vehicleObstruction", "damagedVehicle")),
    ("vehicleObstruction", {"vehicleObstructionType": "vehicleInDifficulty"}, ("breakdown", "vehicleObstruction", "vehicleInDifficulty")),
    ("vehicleObstruction", {"vehicleObstructionType": "vehicleStuck"}, ("breakdown", "vehicleObstruction", "vehicleStuck")),
    # no driver to phone / a fire-service job first: logged, but not a lead
    ("vehicleObstruction", {"vehicleObstructionType": "abandonedVehicle"}, ("lane_closure", "vehicleObstruction", "abandonedVehicle")),
    ("vehicleObstruction", {"vehicleObstructionType": "vehicleOnFire"}, ("lane_closure", "vehicleObstruction", "vehicleOnFire")),
    ("vehicleObstruction", {"vehicleObstructionType": ["abnormalLoad", "vehicleStuck"]}, ("breakdown", "vehicleObstruction", "abnormalLoad,vehicleStuck")),
    ("vehicleObstruction", {"vehicleObstructionType": "abnormalLoad"}, ("lane_closure", "vehicleObstruction", "abnormalLoad")),
    ("vehicleObstruction", {"vehicleObstructionType": "convoy"}, ("lane_closure", "vehicleObstruction", "convoy")),
    ("vehicleObstruction", None, ("lane_closure", "vehicleObstruction", None)),
    ("accident", {"accidentType": ["collision"]}, ("accident", "accident", "collision")),
    ("accident", None, ("accident", "accident", None)),
    ("roadMaintenance", {"roadMaintenanceType": ["other"]}, ("lane_closure", "roadMaintenance", "other")),
    ("roadOrCarriagewayOrLaneManagement", {"roadOrCarriagewayOrLaneManagementType": {"value": "laneClosures"}}, ("lane_closure", "roadOrCarriagewayOrLaneManagement", "laneClosures")),
    ("obstruction", {"obstructionType": ["debris"]}, ("lane_closure", "obstruction", "debris")),
    (None, None, ("lane_closure", None, None)),
])
def test_classify_rules(cause_type, detailed, expected):
    assert nh.classify(make_record(cause_type, detailed)) == expected


def test_breakdown_causes_constant():
    assert nh.BREAKDOWN_CAUSES == {"brokenDownVehicle", "damagedVehicle", "vehicleInDifficulty",
                                   "vehicleStuck"}
    assert "abandonedVehicle" not in nh.BREAKDOWN_CAUSES
    assert "vehicleOnFire" not in nh.BREAKDOWN_CAUSES


def test_classify_falls_back_to_record_type_only_without_a_cause_block():
    rec = make_record(vehicleObstructionType="brokenDownVehicle")
    assert nh.classify(rec, "sitVehicleObstruction") == ("breakdown", "vehicleObstruction", "brokenDownVehicle")
    rec = make_record(accidentType=["collision"])
    assert nh.classify(rec, "sitAccident") == ("accident", "accident", "collision")
    # an explicit cause always wins over the record type
    rec = make_record("roadMaintenance", {"roadMaintenanceType": ["other"]}, accidentType=["collision"])
    assert nh.classify(rec, "sitAccident") == ("lane_closure", "roadMaintenance", "other")
    # unknown record type with no cause: nothing is invented
    assert nh.classify(make_record(), "sitSomethingElse") == ("lane_closure", None, None)


def test_position_rules():
    # only the hard shoulder closed -> shoulder (enum value, and NH's lh/rh codes)
    assert nh.position(make_record(lanes=[lane(0, "hardShoulder", "closed", ext=False)])) == "shoulder"
    assert nh.position(make_record(lanes=[lane(0, "lh", "closed")])) == "shoulder"
    assert nh.position(make_record(lanes=[lane(0, "emergencyLane", "closed", ext=False),
                                          lane(1, "cl1", "open")])) == "shoulder"
    assert nh.position(make_record(lanes=[lane(0, "layBy", "closed", ext=False)])) == "shoulder"
    assert nh.position(make_record(lanes=[lane(0, "verge", "closed", ext=False)])) == "shoulder"
    # a running lane closed -> in_lane, even together with the shoulder
    assert nh.position(make_record(lanes=[lane(1, "cl1", "closed")])) == "in_lane"
    assert nh.position(make_record(lanes=[lane(0, "lh", "closed"), lane(1, "cl1", "closed")])) == "in_lane"
    assert nh.position(make_record(lanes=[lane(1, "leftLane", "closed", ext=False)])) == "in_lane"
    # no closed lane but numberOfLanesRestricted >= 1 -> in_lane
    assert nh.position(make_record(lanes=[lane(1, "cl1", "narrow")], restricted=1)) == "in_lane"
    assert nh.position(make_record(restricted=2)) == "in_lane"
    # NH never counts the hard shoulder in numberOfLanesRestricted, so a
    # positive count means a running lane is affected even when the lane
    # list only mentions the shoulder
    assert nh.position(make_record(lanes=[lane(0, "lh", "closed")], restricted=1)) == "in_lane"
    assert nh.position(make_record(lanes=[lane(0, "hardShoulder", "closed", ext=False)], restricted=1)) == "in_lane"
    # a count of 0 (or none) falls through to the lane list
    assert nh.position(make_record(lanes=[lane(0, "lh", "closed")], restricted=0)) == "shoulder"
    assert nh.position(make_record(lanes=[lane(1, "cl1", "closed")], restricted=0)) == "in_lane"
    # nothing closed, nothing restricted -> unknown
    assert nh.position(make_record()) == "unknown"
    assert nh.position(make_record(lanes=[lane(1, "cl1", "open")], restricted=0)) == "unknown"
    assert nh.position(make_record(lanes=[lane(1, "cl1", "opened")])) == "unknown"
    assert nh.position({}) == "unknown"


def test_to_report_source_id_falls_back_to_situation_and_index():
    rec = make_record("vehicleObstruction", {"vehicleObstructionType": "brokenDownVehicle"})
    del rec["idG"]
    r = nh.to_report("SIT-9", "2025-03-20T11:30:37Z", "sitRoadOrCarriagewayOrLaneManagement", rec, index=3)
    assert r.source_id == "SIT-9/3"
    assert r.kind == "breakdown"
    assert (r.lat, r.lon) == (51.45, -0.41)
    # no validity -> reported_at from situationRecordCreationTime
    assert r.reported_at == datetime(2025, 3, 20, 11, 28, 24, tzinfo=timezone.utc)
    assert r.road is None and r.direction is None      # nothing invented
    assert r.description is None
    assert r.extra["management_type"] is None
    assert r.extra["validity_status"] is None
    assert r.extra["end_time"] is None
    assert r.extra["start_time"] is None
    assert r.extra["creation_time"] == "2025-03-20T11:28:24Z"
    assert r.extra["road_names"] == [] and r.extra["directions_raw"] == [] and r.extra["lines"] == []


def test_to_report_reported_at_prefers_overall_start_time():
    rec = make_record(validity={"validityStatus": "active",
                                "validityTimeSpecification": {"overallStartTime": "2025-03-20T10:00:00.50Z",
                                                              "overallEndTime": "2025-03-20T12:00:00Z"}})
    r = nh.to_report("S", None, "sitRoadOrCarriagewayOrLaneManagement", rec)
    assert r.reported_at == datetime(2025, 3, 20, 10, 0, 0, 500000, tzinfo=timezone.utc)
    assert r.extra["validity_status"] == "active"
    assert r.extra["end_time"] == "2025-03-20T12:00:00Z"
    assert r.extra["start_time"] == "2025-03-20T10:00:00.50Z"


def test_to_report_reported_at_never_lies_in_the_future():
    created = datetime(2025, 3, 20, 11, 28, 24, tzinfo=timezone.utc)
    # a planned / future start: the creation time is the report time
    rec = make_record(validity={"validityStatus": "planned",
                                "validityTimeSpecification": {"overallStartTime": "2099-01-01T00:00:00Z",
                                                              "overallEndTime": "2099-01-02T00:00:00Z"}})
    r = nh.to_report("S", None, "sitRoadOrCarriagewayOrLaneManagement", rec)
    assert r.reported_at == created
    assert r.extra["start_time"] == "2099-01-01T00:00:00Z"   # raw value kept for the probe
    assert r.extra["validity_status"] == "planned"
    # the start is a little after creation (the usual live case): creation wins
    rec = make_record(validity={"validityTimeSpecification": {"overallStartTime": "2025-03-20T11:29:00Z"}})
    assert nh.to_report("S", None, "k", rec).reported_at == created
    # a future start and no creation time: nothing is trusted, so unknown
    rec = make_record(validity={"validityTimeSpecification": {"overallStartTime": "2099-01-01T00:00:00Z"}})
    del rec["situationRecordCreationTime"]
    assert nh.to_report("S", None, "k", rec).reported_at is None
    # a start only, before the version time: it is used
    rec = make_record(validity={"validityTimeSpecification": {"overallStartTime": "2025-03-20T11:00:00Z"}})
    del rec["situationRecordCreationTime"]
    assert nh.to_report("S", None, "k", rec).reported_at == datetime(2025, 3, 20, 11, 0, tzinfo=timezone.utc)
    # no version time at all: min(start, created) still applies
    rec = make_record(validity={"validityTimeSpecification": {"overallStartTime": "2099-01-01T00:00:00Z"}})
    del rec["situationRecordVersionTime"]
    assert nh.to_report("S", None, "k", rec).reported_at == created


def test_parse_iso_variants():
    assert nh.parse_iso("2025-03-20T11:03:12Z") == datetime(2025, 3, 20, 11, 3, 12, tzinfo=timezone.utc)
    assert nh.parse_iso("2025-03-21T18:15:56.56Z") == datetime(2025, 3, 21, 18, 15, 56, 560000, tzinfo=timezone.utc)
    assert nh.parse_iso("2025-03-21T18:15:56") == datetime(2025, 3, 21, 18, 15, 56, tzinfo=timezone.utc)
    assert nh.parse_iso("2025-03-21T18:15:56+01:00") == datetime(2025, 3, 21, 17, 15, 56, tzinfo=timezone.utc)
    assert nh.parse_iso(None) is None
    assert nh.parse_iso("") is None
    assert nh.parse_iso("not a date") is None


def test_records_without_coordinates_are_skipped_and_counted(incident):
    payload = copy.deepcopy(incident)
    rec = payload["D2Payload"]["situation"][0]["situationRecord"][0]["sitRoadOrCarriagewayOrLaneManagement"]
    del rec["locationReference"]["locPointLocation"]["pointByCoordinates"]
    res = nh.parse_payload_detailed(payload)
    assert res.n_records == 1
    assert res.n_skipped_no_coords == 1
    assert res.reports == [] and res.n_errors == 0
    with pytest.raises(nh.NoCoordinates):
        nh.to_report("S", None, "sitRoadOrCarriagewayOrLaneManagement", rec)
    # the road and direction were still readable
    loc = nh.parse_location(rec)
    assert loc["road"] == "M62" and loc["direction"] == "southbound" and loc["points"] == []


def test_iter_records_walks_every_record_type_key(incident):
    payload = copy.deepcopy(incident)
    sit = payload["D2Payload"]["situation"][0]
    base = sit["situationRecord"][0]["sitRoadOrCarriagewayOrLaneManagement"]
    accident = copy.deepcopy(base)
    accident["idG"] = "acc-1"
    accident.pop("cause")
    accident["accidentType"] = ["collision"]
    sit["situationRecord"].append({"sitAccident": accident})
    sit["situationRecord"].append({"sitSomethingNew": {"idG": "x-1"}})  # no location at all
    rows = list(nh.iter_records(payload))
    assert [row[2] for row in rows] == ["sitRoadOrCarriagewayOrLaneManagement", "sitAccident", "sitSomethingNew"]
    assert all(row[0] == "000007-20032025 - 110312" for row in rows)
    assert all(row[1] == "2025-03-20T11:30:37Z" for row in rows)
    res = nh.parse_payload_detailed(payload)
    assert res.n_records == 3
    assert res.n_skipped_no_coords == 1
    assert [r.kind for r in res.reports] == ["breakdown", "accident"]
    assert res.reports[1].source_id == "acc-1"
    dv = nh.distinct_values(payload)
    assert dv["record_type"] == Counter({"sitRoadOrCarriagewayOrLaneManagement": 1, "sitAccident": 1, "sitSomethingNew": 1})
    assert dv["cause_type"] == Counter({"vehicleObstruction": 1, "accident": 1, "(missing)": 1})
    assert dv["shape"] == Counter({"point": 2, "(missing)": 1})


def test_parse_location_group_with_a_point_member():
    rec = {
        "locationReference": {
            "locLocationGroupByList": {
                "locationContainedInGroup": [
                    {"locLinearLocation": {"gmlLineString": {"locGmlLineString": {"posList": "51.5 -0.5 51.51 -0.49"}}},
                     "locSingleRoadLinearLocation": {"linearWithinLinearElement": [
                         {"directionOnLinearSection": "clockwise",
                          "linearElement": {"locLinearElementByCode": {"roadName": "M25"}}}]}},
                    {"locPointLocation": {"pointByCoordinates": {"pointCoordinates": {"latitude": 51.52, "longitude": -0.48}}}},
                ]
            }
        }
    }
    loc = nh.parse_location(rec)
    assert loc["shape"] == "group"
    assert loc["lines"] == [[(-0.5, 51.5), (-0.49, 51.51)], [(-0.48, 51.52)]]
    assert loc["points"] == [(-0.5, 51.5), (-0.49, 51.51), (-0.48, 51.52)]
    assert loc["road"] == "M25" and loc["direction"] == "clockwise"
    assert (loc["lon"], loc["lat"]) == (-0.49, 51.51)


def group_record(members):
    """A location group of linear members, each (posList, direction, road)."""
    contained = []
    for poslist, direction, road in members:
        contained.append({
            "locLinearLocation": {"gmlLineString": {"locGmlLineString": {"posList": poslist}}},
            "locSingleRoadLinearLocation": {"linearWithinLinearElement": [
                {"directionOnLinearSection": direction,
                 "linearElement": {"locLinearElementByCode": {"roadName": road}}}]},
        })
    return {"idG": "grp-1", "situationRecordCreationTime": "2025-03-20T11:28:24Z",
            "locationReference": {"locLocationGroupByList": {"locationContainedInGroup": contained}}}


def test_group_members_with_opposite_directions_give_no_direction():
    # NH models "both directions" as one member per carriageway
    rec = group_record([("51.5 -0.5 51.51 -0.49", "eastBound", "A120"),
                        ("51.51 -0.49 51.5 -0.5", "westBound", "A120")])
    loc = nh.parse_location(rec)
    assert loc["direction"] is None                    # never the first member's value
    assert loc["directions_raw"] == ["eastBound", "westBound"]
    assert loc["direction_raw"] == "eastBound"
    assert loc["road"] == "A120"                       # both members agree on the road
    r = nh.to_report("S", None, "sitRoadOrCarriagewayOrLaneManagement", rec)
    assert r.direction is None and r.road == "A120"
    assert r.extra["directions_raw"] == ["eastBound", "westBound"]
    assert r.extra["road_names"] == ["A120"]
    assert [len(m) for m in r.extra["lines"]] == [2, 2]
    assert len(r.line) == 4
    dv = nh.distinct_values(rec and {"situation": [{"idG": "s", "situationRecord": [{"sitX": rec}]}]})
    assert dv["direction"] == Counter({"eastBound": 1, "westBound": 1})
    assert dv["direction_normalised"] == Counter({"(missing)": 1})


def test_group_members_with_different_roads_give_no_road():
    rec = group_record([("51.5 -0.5 51.51 -0.49", "eastBound", "A120"),
                        ("51.51 -0.49 51.5 -0.5", "eastBound", "A12")])
    loc = nh.parse_location(rec)
    assert loc["road"] is None
    assert loc["road_names"] == ["A120", "A12"]
    assert loc["direction"] == "eastbound"             # the members agree on that
    r = nh.to_report("S", None, "k", rec)
    assert r.road is None and r.direction == "eastbound"
    assert r.extra["road_names"] == ["A120", "A12"]


def test_group_direction_rules_keep_agreement_and_feed_said_both():
    # same readable value twice (plus a value that carries no direction) -> kept
    rec = group_record([("51.5 -0.5 51.51 -0.49", "clockwise", "M25"),
                        ("51.51 -0.49 51.52 -0.48", "clockwise", "M25"),
                        ("51.52 -0.48 51.53 -0.47", "aligned", "M25")])
    assert nh.parse_location(rec)["direction"] == "clockwise"
    # "both" only when the feed itself says so
    rec = group_record([("51.5 -0.5 51.51 -0.49", "bothWays", "M25")])
    assert nh.parse_location(rec)["direction"] == "both"
    # eastBound + bothWays disagree too: None, nothing invented
    rec = group_record([("51.5 -0.5 51.51 -0.49", "eastBound", "M25"),
                        ("51.5 -0.5 51.51 -0.49", "allDirections", "M25")])
    assert nh.parse_location(rec)["direction"] is None
    # the enum's own "unknown" is None on the Report, like every other unreadable value
    rec = make_record()
    rec["locationReference"]["locPointLocation"]["pointAlongLinearElement"] = [{"directionAtPoint": "unknown"}]
    r = nh.to_report("S", None, "k", rec)
    assert r.direction is None and r.extra["direction_raw"] == "unknown"


def test_malformed_records_do_not_raise_or_lose_the_summary(incident, monkeypatch):
    payload = copy.deepcopy(incident)
    sit = payload["D2Payload"]["situation"][0]
    base = sit["situationRecord"][0]["sitRoadOrCarriagewayOrLaneManagement"]
    # strings / lists where objects are expected, in every place the walkers look
    bad_lane = copy.deepcopy(base)
    bad_lane["idG"] = "bad-lane"
    cw = bad_lane["locationReference"]["locPointLocation"]["supplementaryPositionalDescription"]["carriageway"][0]
    cw["lane"][0]["laneExtensionG"] = "closed"
    cw["carriagewayExtensionG"] = ["x"]
    bad_point = copy.deepcopy(base)
    bad_point["idG"] = "bad-point"
    bad_point["locationReference"]["locPointLocation"]["pointByCoordinates"] = [1, 2]
    bad_line = {"idG": "bad-line", "validity": "active", "source": ["x"], "cause": "vehicleObstruction",
                "locationReference": {"locLinearLocation": {"gmlLineString": "51.5 -0.5"}}}
    sit["situationRecord"] += [{"sitRoadOrCarriagewayOrLaneManagement": bad_lane},
                               {"sitRoadOrCarriagewayOrLaneManagement": bad_point},
                               {"sitVehicleObstruction": bad_line}]
    res = nh.parse_payload_detailed(payload)
    assert res.n_records == 4 and res.n_errors == 0 and res.errors == []
    assert res.n_skipped_no_coords == 2                # bad-point and bad-line have no usable position
    assert [r.source_id for r in res.reports] == [base["idG"], "bad-lane"]
    assert res.reports[1].extra["lanes"][0]["status"] is None
    assert res.reports[1].extra["lanes_restricted"] is None
    dv = nh.distinct_values(payload)
    assert dv["errors"] == Counter()
    assert dv["record_type"] == Counter({"sitRoadOrCarriagewayOrLaneManagement": 3, "sitVehicleObstruction": 1})
    assert dv["cause_type"]["vehicleObstruction"] == 4  # the record-type fallback still classifies bad-line
    # a record that raises anyway is counted under "errors" and the rest is still summarised
    real = nh.classify

    def boom(record, key=None):
        if record.get("idG") == "bad-lane":     # has coordinates, so to_report reaches classify
            raise RuntimeError("synthetic")
        return real(record, key)

    monkeypatch.setattr(nh, "classify", boom)
    dv = nh.distinct_values(payload)
    assert dv["errors"] == Counter({"sitRoadOrCarriagewayOrLaneManagement: RuntimeError": 1})
    assert dv["record_type"] == Counter({"sitRoadOrCarriagewayOrLaneManagement": 2, "sitVehicleObstruction": 1})
    assert dv["kind"]["breakdown"] >= 1
    res = nh.parse_payload_detailed(payload)
    assert res.n_errors == 1 and "RuntimeError" in res.errors[0]
    assert [r.source_id for r in res.reports] == [base["idG"]]


def test_report_direction_never_comes_from_free_text():
    rec = make_record()
    rec["locationReference"]["locPointLocation"]["supplementaryPositionalDescription"]["locationDescription"] = "M25 clockwise J13 to J14"
    rec["generalPublicComment"] = [{"comment": "M25 clockwise between J13 and J14"}]
    r = nh.to_report("S", None, "sitRoadOrCarriagewayOrLaneManagement", rec)
    assert r.direction is None
    assert r.road is None
    assert r.extra["location_description"] == "M25 clockwise J13 to J14"
