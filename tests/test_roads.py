"""RoadIndex over the committed data/roads.json."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rgalerts.roads import RoadIndex, Way, _make_way

ROOT = Path(__file__).resolve().parents[1]
ROADS = ROOT / "data" / "roads.json"
FELTHAM = (51.4489, -0.4094)


@pytest.fixture(scope="module")
def index() -> RoadIndex:
    return RoadIndex.load(ROADS)


def _vertex(index: RoadIndex, road: str) -> tuple[float, float]:
    """(lat, lon) of a middle vertex of the first way on ``road``."""
    way = next(w for w in index.ways if w.road == road)
    lon, lat = way.coords[len(way.coords) // 2]
    return lat, lon


def test_data_file_shape():
    data = json.loads(ROADS.read_text(encoding="utf-8"))
    assert "OpenStreetMap" in data["attribution"]
    assert set(data["roads"]) == {"M25", "M3", "M4", "A316"}
    assert data["box"] == {"west": -0.7, "south": 51.36, "east": -0.25, "north": 51.6}
    assert data["source"]["mode"] == "fixture" and data["source"]["box"] == data["box"]
    for road, ways in data["roads"].items():
        assert ways, f"no ways for {road}"
        for w in ways:
            assert {"osm_id", "highway", "oneway", "name", "lanes", "coords"} <= set(w)
            assert w["oneway"] in (True, False, None)
            assert w["lanes"] is None or (isinstance(w["lanes"], int) and not isinstance(w["lanes"], bool))
            assert len(w["coords"]) >= 2
            for lon, lat in w["coords"]:
                assert -8 < lon < 2 and 50 < lat < 59, "coords are [lon, lat]"


def test_load(index: RoadIndex):
    assert index.roads == ["A316", "M25", "M3", "M4"]
    assert len(index) > 500
    assert "OpenStreetMap" in index.attribution
    w = index.ways[0]
    assert w.bbox[0] <= w.bbox[2] and w.bbox[1] <= w.bbox[3]


def test_make_way_lanes_and_oneway_are_typed():
    raw = {"osm_id": 1, "coords": [[-0.5, 51.5], [-0.49, 51.5]], "lanes": 3, "oneway": True}
    assert _make_way("m4", raw).lanes == 3
    assert _make_way("m4", raw).road == "M4"
    # a hand-written `lanes: true` is not one lane, and "3" (a string) is not trusted either
    assert _make_way("M4", dict(raw, lanes=True)).lanes is None
    assert _make_way("M4", dict(raw, lanes="3")).lanes is None
    assert _make_way("M4", dict(raw, oneway="yes")).oneway is None
    assert _make_way("M4", dict(raw, coords=[[-0.5, 51.5]])) is None


def test_m25_vertex_is_on_m25(index: RoadIndex):
    lat, lon = _vertex(index, "M25")
    hit = index.nearest(lat, lon)
    assert hit is not None
    assert hit["road"] == "M25"
    assert hit["distance_m"] < 5
    assert 0 <= hit["segment_bearing"] < 360
    assert hit["oneway"] in (True, False, None)
    assert isinstance(hit["osm_id"], int)


def test_point_just_off_the_road(index: RoadIndex):
    lat, lon = _vertex(index, "M3")
    # ~60 m north of the vertex: found at 100 m, not at 30 m
    hit = index.nearest(lat + 60 / 111_320, lon, max_m=100)
    assert hit is not None and hit["road"] == "M3"
    assert 40 < hit["distance_m"] < 80
    assert index.nearest(lat + 60 / 111_320, lon, max_m=30) is None


def test_feltham_base_is_not_on_a_configured_road(index: RoadIndex):
    # The base is ~2 km north of the A316 (Country Way), so nothing within 100 m.
    assert index.nearest(*FELTHAM, max_m=100) is None
    far = index.nearest(*FELTHAM, max_m=5000)
    assert far is not None and far["road"] == "A316" and far["distance_m"] > 1000


def test_on_listed_road_rejects_other_roads(index: RoadIndex):
    lat, lon = _vertex(index, "M4")
    assert index.on_listed_road(lat, lon, ["M25"]) is None
    assert index.on_listed_road(lat, lon, ["M4"]) == "M4"
    assert index.on_listed_road(lat, lon, ["m25", "m4"]) == "M4"
    assert index.on_listed_road(lat, lon, []) is None


def test_nearest_roads_filter(index: RoadIndex):
    lat, lon = _vertex(index, "A316")
    assert index.nearest(lat, lon, roads=["A316"])["road"] == "A316"
    assert index.nearest(lat, lon, roads=["M25"]) is None


def test_segment_bearing_follows_node_order():
    # a due-east two-point way
    w = Way("M4", 1, True, "motorway", None, 3, ((-0.50, 51.50), (-0.49, 51.50)), (-0.50, 51.50, -0.49, 51.50))
    idx = RoadIndex([w])
    hit = idx.nearest(51.5001, -0.495)
    assert hit is not None and abs(hit["segment_bearing"] - 90) < 1
    # reversed node order -> due west
    w2 = Way("M4", 2, True, "motorway", None, 3, ((-0.49, 51.50), (-0.50, 51.50)), (-0.50, 51.50, -0.49, 51.50))
    hit2 = RoadIndex([w2]).nearest(51.5001, -0.495)
    assert abs(hit2["segment_bearing"] - 270) < 1
