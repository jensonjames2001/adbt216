"""Junction data + labeller. Uses the committed data/junctions.json and
data/roads.json (built with scripts/fetch_junctions.py --from-fixture), plus
unit tests of the script's build functions on a synthetic Overpass reply."""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

from rgalerts.junctions import (
    AT_JUNCTION_M, NEAR_M, Junction, Junctions, is_motorway, junction_ref_key, normalise_ref,
)

ROOT = Path(__file__).resolve().parents[1]
JUNCTIONS = ROOT / "data" / "junctions.json"
ROADS = ROOT / "data" / "roads.json"
SCRIPT = ROOT / "scripts" / "fetch_junctions.py"


def _haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


@pytest.fixture(scope="module")
def junctions() -> Junctions:
    return Junctions.load(JUNCTIONS)


@pytest.fixture(scope="module")
def roads_data() -> dict:
    return json.loads(ROADS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def road_coords(roads_data) -> dict[str, list[tuple[float, float]]]:
    """road -> every (lon, lat) vertex of its ways."""
    return {road: [tuple(c) for w in ways for c in w["coords"]] for road, ways in roads_data["roads"].items()}


@pytest.fixture(scope="module")
def fetch_junctions():
    """The script scripts/fetch_junctions.py imported as a module."""
    spec = importlib.util.spec_from_file_location("fetch_junctions", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _nudge_to_road(lat: float, lon: float, road_coords, road: str) -> tuple[float, float]:
    """Move (lat, lon) onto the nearest vertex of ``road``. Returns (lat, lon)."""
    lon2, lat2 = min(road_coords[road], key=lambda c: _haversine_m(lat, lon, c[1], c[0]))
    return lat2, lon2


# --- data file -------------------------------------------------------------

def test_data_file_has_attribution_and_shape():
    data = json.loads(JUNCTIONS.read_text(encoding="utf-8"))
    assert "OpenStreetMap" in data["attribution"]
    assert "ODbL" in data["attribution"]
    assert data["generated_at"]
    assert data["box"]["west"] < data["box"]["east"]
    assert {"osm_id", "lat", "lon", "ref", "name", "road", "roads"} <= set(data["junctions"][0])
    # no personal data, only OSM tags: every key is one of the documented ones
    for j in data["junctions"]:
        assert set(j) == {"osm_id", "lat", "lon", "ref", "name", "road", "roads"}


def test_data_file_built_from_fixture_records_the_fixture_box():
    data = json.loads(JUNCTIONS.read_text(encoding="utf-8"))
    assert data["source"]["mode"] == "fixture"
    assert data["source"]["box"] == data["box"]
    assert data["source"]["roads"] == ["M25", "M3", "M4", "A316"]
    assert data["roads"] == ["M25", "M3", "M4", "A316"]


def test_loaded_attribution(junctions: Junctions):
    assert "OpenStreetMap" in junctions.attribution
    assert len(junctions) > 90


def test_m25_refs(junctions: Junctions):
    refs = {j.ref for j in junctions.junctions_on("M25")}
    assert {"11", "12", "13", "14", "15", "16"} <= refs


def test_m4_refs(junctions: Junctions):
    refs = {j.ref for j in junctions.junctions_on("M4")}
    assert {"1", "2", "3", "4", "5", "6", "7", "4A", "4B"} <= refs


def test_m3_refs(junctions: Junctions):
    refs = {j.ref for j in junctions.junctions_on("M3")}
    assert {"1", "2"} <= refs


def test_junctions_are_grouped_and_sorted(junctions: Junctions):
    js = junctions.junctions_on("M4")
    refs = [j.ref for j in js if j.ref]
    assert refs == sorted(refs, key=junction_ref_key)
    assert len(refs) == len(set(refs)), "one entry per ref"
    j4 = next(j for j in js if j.ref == "4")
    assert j4.n_nodes >= 2 and len(j4.osm_ids) == j4.n_nodes
    assert j4.name == "Heathrow"
    assert j4.display == "J4 (Heathrow)"
    # unnumbered named junctions sort last and are excluded on request
    assert all(j.numbered for j in junctions.junctions_on("M4", include_unnumbered=False))
    if any(not j.numbered for j in js):
        assert not js[-1].numbered


def test_road_matching_is_case_insensitive(junctions: Junctions):
    assert [j.ref for j in junctions.junctions_on("m25")] == [j.ref for j in junctions.junctions_on("M25")]


def test_a_road_junctions_are_named_not_numbered(junctions: Junctions):
    # OSM tags the A316 node at Sunbury Cross with the M3's junction number "1";
    # there is no "A316 J1" on any sign, so A-roads show names only.
    a316 = junctions.junctions_on("A316")
    assert all(j.ref is None for j in a316)
    assert "Sunbury Cross" in {j.name for j in a316}
    assert all(not j.display.startswith("J") for j in a316)
    # the same node still gives the M3 its J1
    assert "J1 (Sunbury Cross)" in {j.display for j in junctions.junctions_on("M3")}
    # M4 J1 shares a node with the A4: numbered on the M4, named only on the A4
    assert "J1 (Chiswick Roundabout)" in {j.display for j in junctions.junctions_on("M4")}
    assert [j.display for j in junctions.junctions_on("A4")] == ["Chiswick Roundabout"]


# --- labeller -------------------------------------------------------------

def test_between_m25_j13_j14(junctions: Junctions, road_coords):
    m25 = {j.ref: j for j in junctions.junctions_on("M25")}
    j13, j14 = m25["13"], m25["14"]
    lat, lon = _nudge_to_road((j13.lat + j14.lat) / 2, (j13.lon + j14.lon) / 2, road_coords, "M25")
    r = junctions.between(lat, lon, "M25")
    assert r["form"] == "span"
    assert r["a"] is not None and r["b"] is not None
    assert {r["a"].ref, r["b"].ref} == {"13", "14"}
    # ascending junction order, direction of travel is not decided here
    assert r["a"].ref == "13" and r["b"].ref == "14"
    assert r["label"] == "J13 (Runnymede) to J14 (Poyle)"
    assert r["nearest"] in (r["a"], r["b"])
    assert 0 < r["distance_a_m"] < 3000 and 0 < r["distance_b_m"] < 3000


def test_between_m4_at_j4_is_near_not_a_span(junctions: Junctions, road_coords):
    # the M4 vertex closest to the J4 centroid (a few metres away) used to come
    # out as "J4 (Heathrow) to J4B (Thorney)", a 2.8 km span
    j4 = next(j for j in junctions.junctions_on("M4") if j.ref == "4")
    lat, lon = _nudge_to_road(j4.lat, j4.lon, road_coords, "M4")
    r = junctions.between(lat, lon, "M4")
    assert r["form"] == "near"
    assert r["b"] is None
    assert r["label"].startswith("near J4 (Heathrow)")
    assert r["nearest"].ref == "4" and r["a"].ref == "4"
    assert r["distance_a_m"] < AT_JUNCTION_M
    # the mainline vertices the reviewer flagged: 70 m and 24 m from the centroid
    for lat, lon in [(51.49574, -0.45269), (51.49534, -0.45264)]:
        assert junctions.between(lat, lon, "M4")["label"] == "near J4 (Heathrow)"


def test_between_m4_1km_west_of_j4(junctions: Junctions, road_coords):
    j4 = next(j for j in junctions.junctions_on("M4") if j.ref == "4")
    lat, lon = _nudge_to_road(j4.lat, j4.lon - 1000 / (111_320 * math.cos(math.radians(j4.lat))),
                              road_coords, "M4")
    assert 800 < _haversine_m(lat, lon, j4.lat, j4.lon) < 1200
    r = junctions.between(lat, lon, "M4")
    assert r["form"] == "span"
    assert r["label"] == "J4 (Heathrow) to J4B (Thorney)"
    assert r["nearest"].ref == "4"


def test_between_m4_east_of_j4_is_j3_to_j4_not_the_spur(junctions: Junctions):
    # 594 m east of J4 on the mainline used to be "J4 to J4A (Concorde Roundabout)",
    # but J4A is on the airport spur; the point is between J3 and J4
    r = junctions.between(51.495, -0.44407, "M4")
    assert r["label"] == "J3 (Cranford Parkway) to J4 (Heathrow)"


def test_between_huntercombe_spur_is_near_or_beyond_j7(junctions: Junctions, roads_data):
    # the spur north of J7 has no further junction: never a span to J4B or J6
    j7 = next(j for j in junctions.junctions_on("M4") if j.ref == "7")
    spur = [w for w in roads_data["roads"]["M4"] if "Huntercombe" in (w.get("name") or "")]
    assert spur, "the fixture has ways named Huntercombe Spur"
    north = [(lat, lon) for w in spur for lon, lat in w["coords"] if lat > j7.lat + 0.001]
    assert north
    seen = set()
    for lat, lon in north:
        r = junctions.between(lat, lon, "M4")
        assert r["b"] is None and r["a"].ref == "7", (lat, lon, r["label"])
        assert r["label"] in ("near J7 (Huntercombe Spur)", "beyond J7 (Huntercombe Spur)")
        assert r["form"] == ("near" if r["distance_a_m"] < NEAR_M else "beyond")
        seen.add(r["form"])
    assert seen == {"near", "beyond"}
    # the reviewer's example, 900 m up the spur
    assert junctions.between(51.51963, -0.65459, "M4")["label"] == "beyond J7 (Huntercombe Spur)"


def test_between_beyond_last_junction(junctions: Junctions):
    # north of M25 J16 (Denham) there is no further junction in the box; 2 km
    # past it is "beyond", not "near", and Phase 1 shows the distance
    j16 = next(j for j in junctions.junctions_on("M25") if j.ref == "16")
    r = junctions.between(j16.lat + 0.02, j16.lon - 0.002, "M25")
    assert r["a"].ref == "16" and r["b"] is None
    assert r["form"] == "beyond"
    assert r["label"] == "beyond J16 (Denham)"
    assert r["distance_a_m"] > NEAR_M
    # 300 m past it is still "near"
    r = junctions.between(j16.lat + 300 / 111_320, j16.lon, "M25")
    assert r["form"] == "near" and r["label"] == "near J16 (Denham)"


def test_between_at_a_junction_says_near(junctions: Junctions):
    j13 = next(j for j in junctions.junctions_on("M25") if j.ref == "13")
    r = junctions.between(j13.lat, j13.lon, "M25")
    assert r["a"].ref == "13" and r["b"] is None
    assert r["form"] == "near"
    assert r["label"].startswith("near J13")


def test_between_unknown_road_is_none(junctions: Junctions):
    r = junctions.between(51.45, -0.45, "M1")
    assert r == {"a": None, "b": None, "label": None, "form": None, "nearest": None,
                 "distance_a_m": None, "distance_b_m": None, "road": "M1"}
    assert junctions.between(51.45, -0.45, None)["label"] is None


def test_between_never_skips_a_junction_or_says_near_from_afar(junctions: Junctions, roads_data):
    """Every vertex of every configured road: a span's two junctions have no
    other junction on the way between them, 'near' means under NEAR_M, and
    'beyond' means nothing lies on the far side."""
    for road, ways in roads_data["roads"].items():
        js = junctions.junctions_on(road)
        for w in ways:
            for lon, lat in w["coords"][::2]:
                r = junctions.between(lat, lon, road)
                assert r["form"] in ("span", "near", "beyond"), (road, lat, lon)
                if r["form"] == "near":
                    assert r["b"] is None and r["distance_a_m"] < NEAR_M, (road, lat, lon, r["label"])
                    assert r["label"] == f"near {r['a'].display}"
                elif r["form"] == "beyond":
                    assert r["b"] is None and r["distance_a_m"] >= NEAR_M, (road, lat, lon, r["label"])
                    assert r["label"] == f"beyond {r['a'].display}"
                else:
                    a, b = r["a"], r["b"]
                    assert a.sort_key <= b.sort_key
                    assert r["label"] == f"{a.display} to {b.display}"
                    d_ab = _haversine_m(a.lat, a.lon, b.lat, b.lon)
                    for c in js:
                        if c is a or c is b:
                            continue
                        via = _haversine_m(a.lat, a.lon, c.lat, c.lon) + _haversine_m(c.lat, c.lon, b.lat, b.lon)
                        assert via > 1.2 * d_ab, f"{road} ({lat},{lon}) {r['label']} skips {c.display}"


def test_nearest_is_sorted(junctions: Junctions):
    got = junctions.nearest(51.4489, -0.4094, "M4", n=3)
    assert len(got) == 3
    assert all(isinstance(j, Junction) for j, _ in got)
    dists = [d for _, d in got]
    assert dists == sorted(dists)


# --- helpers ---------------------------------------------------------------

def test_junction_ref_key():
    assert junction_ref_key("4A") == (4, "A")
    assert junction_ref_key("13") == (13, "")
    assert junction_ref_key("J4b") == (4, "B")
    assert junction_ref_key("4;4A") == (4, "")
    assert sorted(["5", "4B", "4", "4A", "13", "1"], key=junction_ref_key) == ["1", "4", "4A", "4B", "5", "13"]
    assert junction_ref_key(None) > junction_ref_key("999")
    assert junction_ref_key("odd") > junction_ref_key("999")


def test_normalise_ref():
    assert normalise_ref(" 4a ") == "4A"
    assert normalise_ref("J13") == "13"
    assert normalise_ref("") is None and normalise_ref(None) is None
    assert normalise_ref("4;4a") == "4;4A"
    assert normalise_ref(";") is None


def test_is_motorway():
    assert is_motorway("M25") and is_motorway("m4") and is_motorway("A1(M)")
    assert not is_motorway("A316") and not is_motorway("A4") and not is_motorway(None)


def test_display_never_invents():
    assert Junction("M4", "4", None, 0, 0, (1,), 1).display == "J4"
    assert Junction("M4", None, "Heston Services", 0, 0, (1,), 1).display == "Heston Services"
    assert Junction("M4", None, None, 0, 0, (1,), 1).display == "unknown"
    # an OSM multi-value ref is shown as both numbers, not "J4;4A"
    assert Junction("M4", "4;4A", "Heathrow", 0, 0, (1,), 1).display == "J4/J4A (Heathrow)"
    assert Junction("M4", "4;4A", None, 0, 0, (1,), 1).display == "J4/J4A"


def test_grouping_on_synthetic_nodes():
    nodes = [
        {"osm_id": 2, "lat": 51.0, "lon": -0.5, "ref": "4a", "name": None, "road": "M4", "roads": ["M4"]},
        {"osm_id": 1, "lat": 51.0, "lon": -0.5002, "ref": "4A", "name": "Concorde", "road": "M4", "roads": ["M4"]},
        {"osm_id": 3, "lat": 51.0, "lon": -0.6, "ref": "4;4A", "name": None, "road": "M4", "roads": ["M4"]},
        {"osm_id": 4, "lat": 51.0, "lon": -0.7, "ref": "1", "name": "Sunbury Cross", "road": "A316", "roads": ["A316"]},
        {"osm_id": 5, "lat": 51.0, "lon": -0.8, "ref": None, "name": None, "road": "M4", "roads": ["M4"]},
    ]
    js = Junctions(nodes)
    m4 = js.junctions_on("M4")
    # "4;4A" keys as (4, "") so it sorts with J4, before J4A
    assert [j.display for j in m4] == ["J4/J4A", "J4A (Concorde)"]
    assert m4[1].osm_ids == (1, 2) and m4[1].n_nodes == 2
    assert abs(m4[1].lon - (-0.5001)) < 1e-9
    assert js.junctions_on("A316")[0].to_dict()["display"] == "Sunbury Cross"
    assert js.roads == ["A316", "M4"]


# --- scripts/fetch_junctions.py build functions -----------------------------

def test_script_parse_oneway(fetch_junctions):
    fj = fetch_junctions
    assert fj.parse_oneway({"oneway": "yes"}) == (True, False)
    assert fj.parse_oneway({"oneway": "no"}) == (False, False)
    assert fj.parse_oneway({"oneway": "-1"}) == (True, True)
    assert fj.parse_oneway({}) == (None, False)
    assert fj.parse_oneway({"oneway": "reversible"}) == (None, False)
    assert fj.parse_lanes({"lanes": "3"}) == 3
    assert fj.parse_lanes({"lanes": "3;4"}) is None and fj.parse_lanes({}) is None


def test_script_build_roads_filters_and_reverses(fetch_junctions):
    fj = fetch_junctions
    raw = {"elements": [
        {"type": "way", "id": 1, "tags": {"ref": "M4", "highway": "motorway", "oneway": "-1", "lanes": "3"},
         "geometry": [{"lon": -0.50, "lat": 51.50}, {"lon": -0.49, "lat": 51.50}]},
        {"type": "way", "id": 2, "tags": {"ref": "A4", "highway": "trunk"},
         "geometry": [{"lon": -0.50, "lat": 51.51}, {"lon": -0.49, "lat": 51.51}]},
        {"type": "way", "id": 3, "tags": {"ref": "m25", "highway": "motorway", "oneway": "yes"},
         "geometry": [{"lon": -0.50, "lat": 51.52}, {"lon": -0.49, "lat": 51.52}]},
        {"type": "way", "id": 4, "tags": {"ref": "M4", "highway": "motorway", "oneway": "no", "lanes": "x"},
         "geometry": [{"lon": -0.50, "lat": 51.53}]},
        {"type": "node", "id": 5, "lat": 51.5, "lon": -0.5},
    ]}
    out = fj.build_roads(raw, ["M4", "M25"])
    assert set(out) == {"M4", "M25"}
    assert "A4" not in out
    assert len(out["M4"]) == 1, "the one-point way is dropped"
    w = out["M4"][0]
    assert w["osm_id"] == 1 and w["oneway"] is True and w["lanes"] == 3
    assert w["coords"] == [[-0.49, 51.5], [-0.5, 51.5]], "oneway=-1 reverses to the direction of travel"
    assert 690 < w["length_m"] < 700
    assert out["M25"][0]["osm_id"] == 3 and out["M25"][0]["oneway"] is True
    assert out["M25"][0]["coords"] == [[-0.5, 51.52], [-0.49, 51.52]]


def test_script_build_junctions_parent_road_choice(fetch_junctions):
    fj = fetch_junctions
    raw = {"elements": [
        {"type": "node", "id": 10, "lat": 51.50, "lon": -0.50, "tags": {"highway": "motorway_junction", "ref": "4a", "name": " Heathrow "}},
        {"type": "node", "id": 11, "lat": 51.51, "lon": -0.51, "tags": {"highway": "motorway_junction", "ref": "1"}},
        {"type": "node", "id": 12, "lat": 51.52, "lon": -0.52, "tags": {"highway": "motorway_junction"}},
        {"type": "node", "id": 13, "lat": 51.53, "lon": -0.53, "tags": {"highway": "motorway_junction", "ref": "15"}},
        {"type": "way", "id": 100, "tags": {"ref": "A4", "highway": "trunk"}, "nodes": [10, 13, 999]},
        {"type": "way", "id": 101, "tags": {"ref": "M4", "highway": "motorway"}, "nodes": [10, 11]},
        {"type": "way", "id": 102, "tags": {"ref": "M25", "highway": "motorway"}, "nodes": [13]},
        {"type": "way", "id": 103, "tags": {"highway": "motorway_link"}, "nodes": [12]},
    ]}
    out = fj.build_junctions(raw, ["M25", "M4"])
    by_id = {j["osm_id"]: j for j in out}
    assert set(by_id) == {10, 11, 12, 13}
    assert set(by_id[10]) == {"osm_id", "lat", "lon", "ref", "name", "road", "roads"}
    # a configured road wins over an earlier-listed parent
    assert by_id[10]["road"] == "M4" and by_id[10]["roads"] == ["A4", "M4"]
    assert by_id[10]["ref"] == "4A" and by_id[10]["name"] == "Heathrow"
    assert by_id[13]["road"] == "M25" and by_id[13]["roads"] == ["A4", "M25"]
    assert by_id[11] == {"osm_id": 11, "lat": 51.51, "lon": -0.51, "ref": "1", "name": None, "road": "M4", "roads": ["M4"]}
    # no parent way with a ref: kept, road None (nothing invented)
    assert by_id[12]["road"] is None and by_id[12]["roads"] == [] and by_id[12]["ref"] is None
    # sorted: configured roads by ref order, unknown road last
    assert [j["osm_id"] for j in out] == [13, 11, 10, 12]


def test_script_fixture_problems(fetch_junctions):
    fj = fetch_junctions
    from rgalerts.config import Box
    raw_j = {"elements": [{"type": "node", "id": 1, "lat": 51.5, "lon": -0.5}]}
    raw_r = {"elements": [{"type": "way", "id": 2, "tags": {"ref": "M25"}}, {"type": "way", "id": 3, "tags": {"ref": "M4"}}]}
    good = Box(**fj.FIXTURE_BOX)
    assert fj.fixture_problems(raw_j, raw_r, good, ["M25"]) == []
    problems = fj.fixture_problems(raw_j, raw_r, Box(-2.4, 53.4, -2.1, 53.55), ["M25", "M1", "A3(M)"])
    assert len(problems) == 3
    assert "1 of the 1 saved junction nodes fall outside" in problems[1]
    assert problems[2] == "the saved copy has no ways for M1, A3(M)"
    # a box a little wider than the fixture box is a mismatch too
    wider = Box(fj.FIXTURE_BOX["west"] - 0.1, fj.FIXTURE_BOX["south"], fj.FIXTURE_BOX["east"], fj.FIXTURE_BOX["north"])
    assert len(fj.fixture_problems(raw_j, raw_r, wider, ["M25"])) == 1


def _write_config(tmp_path: Path, **overrides) -> Path:
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    for old, new in overrides.items():
        assert old in cfg
        cfg = cfg.replace(old, new)
    p = tmp_path / "config.yaml"
    p.write_text(cfg, encoding="utf-8")
    return p


def test_script_from_fixture_refuses_a_different_box(fetch_junctions, tmp_path, capsys):
    cfg = _write_config(tmp_path, **{"west: -0.70": "west: -2.4", "east: -0.25": "east: -2.1",
                                     "south: 51.36": "south: 53.4", "north: 51.60": "north: 53.55"})
    with pytest.raises(SystemExit) as e:
        fetch_junctions.main(["--config", str(cfg), "--from-fixture"])
    assert "run without --from-fixture" in str(e.value)
    assert "west -0.7, south 51.36, east -0.25, north 51.6" in str(e.value)
    assert not (tmp_path / "data").exists()


def test_script_from_fixture_refuses_an_unfetched_road(fetch_junctions, tmp_path):
    cfg = _write_config(tmp_path, **{"roads: [M25, M3, M4, A316]": "roads: [M25, M1]"})
    with pytest.raises(SystemExit) as e:
        fetch_junctions.main(["--config", str(cfg), "--from-fixture"])
    assert "no ways for M1" in str(e.value)


def test_script_from_fixture_writes_matching_data(fetch_junctions, tmp_path, capsys):
    cfg = _write_config(tmp_path, **{"roads: [M25, M3, M4, A316]": "roads: [M25, A316]"})
    assert fetch_junctions.main(["--config", str(cfg), "--from-fixture", "--summary"]) == 0
    out = capsys.readouterr().out
    assert "no network" in out
    assert "M25: 6 numbered junctions in the box" in out
    assert "A316: 0 numbered junctions" in out and "Sunbury Cross" in out
    assert "OpenStreetMap" in out
    j = json.loads((tmp_path / "data" / "junctions.json").read_text(encoding="utf-8"))
    r = json.loads((tmp_path / "data" / "roads.json").read_text(encoding="utf-8"))
    assert j["box"] == fetch_junctions.FIXTURE_BOX == j["source"]["box"] == r["box"]
    assert j["roads"] == ["M25", "A316"] and set(r["roads"]) == {"M25", "A316"}
    assert j["source"]["mode"] == "fixture" and j["source"]["osm_base"]
    assert len(j["junctions"]) == 103
