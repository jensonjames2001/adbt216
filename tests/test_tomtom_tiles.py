"""Tests for rgalerts.sources.tomtom_tiles using a tile built with the
installed mapbox_vector_tile encoder (no network)."""
from __future__ import annotations

import mapbox_vector_tile
import pytest

from rgalerts import geo
from rgalerts.sources import tomtom_tiles as tt

ZOOM, X, Y = 10, 511, 340

POI_FEATURES = [
    {   # breakdown, single category
        "geometry": {"type": "Point", "coordinates": [1000, 3000]},
        "properties": {
            "icon_category_0": 14, "description_0": "Broken down vehicle",
            "id": "tt-1", "number_of_reports": 2, "poi_type": "start_poi",
            "magnitude": 1, "delay": 0, "road_type": "Motorway",
            "probability_of_occurrence": "certain",
            "last_report_time": "2026-09-30T14:05:00Z",
        },
    },
    {   # accident
        "geometry": {"type": "Point", "coordinates": [2000, 500]},
        "properties": {"icon_category_0": 1, "description_0": "Accident", "id": "tt-2",
                       "poi_type": "standalone_poi"},
    },
    {   # cluster of three, mixed categories
        "geometry": {"type": "Point", "coordinates": [3000, 2048]},
        "properties": {"cluster_id": 5, "cluster_size": 3, "icon_category": 13, "clustered": 1},
    },
    {   # jam + breakdown: multi-category
        "geometry": {"type": "Point", "coordinates": [3500, 3900]},
        "properties": {"icon_category_0": 6, "description_0": "Stationary traffic",
                       "icon_category_1": 14, "description_1": "Broken down vehicle",
                       "id": "tt-3", "poi_type": "start_poi"},
    },
]
FLOW_FEATURES = [
    {
        "geometry": {"type": "LineString", "coordinates": [[1000, 3000], [1200, 3100], [1500, 3300]]},
        "properties": {"icon_category_0": 14, "description_0": "Broken down vehicle", "id": "tt-1",
                       "magnitude": 1},
    },
]


@pytest.fixture(scope="module")
def tile_bytes() -> bytes:
    # y_coord_down=True: the encoder stores our integers verbatim (top-left origin).
    return mapbox_vector_tile.encode(
        [
            {"name": "Traffic incident POI", "features": POI_FEATURES},
            {"name": "Traffic incident flow", "features": FLOW_FEATURES},
        ],
        default_options={"y_coord_down": True},
    )


@pytest.fixture(scope="module")
def features(tile_bytes) -> list[tt.TileFeature]:
    return tt.decode_tile(tile_bytes, ZOOM, X, Y, y_coord_down=True)


def by_id(features, incident_id, layer="poi"):
    return next(f for f in features if f.incident_id == incident_id and f.layer == layer)


# --- URLs --------------------------------------------------------------------

def test_tile_url_carries_key_and_encoded_tags():
    url = tt.tile_url(10, 511, 340, "SECRETKEY")
    assert url.startswith("https://api.tomtom.com/traffic/map/4/tile/incidents/10/511/340.pbf?")
    assert "key=SECRETKEY" in url
    assert "tags=%5Bicon_category%2Cdescription%2C" in url
    assert url.endswith("%2Croad_category%5D")
    assert "[" not in url and "]" not in url and " " not in url


def test_tile_url_for_log_has_no_key():
    url = tt.tile_url_for_log(10, 511, 340)
    assert "key" not in url
    assert "/10/511/340.pbf?tags=" in url
    with pytest.raises(ValueError):
        tt.tile_url(10, 511, 340, "")


@pytest.mark.parametrize("zoom,x,y", [
    (-1, 0, 0), (23, 0, 0),            # zoom outside 0..22
    (10, 1024, 340), (10, 511, 1024),  # x / y one past the last tile
    (10, -1, 340), (10, 511, -1),
    (0, 1, 0),                         # zoom 0 has exactly one tile
])
def test_tile_url_rejects_bad_tile_addresses(zoom, x, y):
    with pytest.raises(ValueError):
        tt.tile_url(zoom, x, y, "SECRETKEY")
    with pytest.raises(ValueError):
        tt.tile_url_for_log(zoom, x, y)
    with pytest.raises(ValueError):
        tt.check_tile(zoom, x, y)


def test_check_tile_accepts_the_edges():
    assert tt.check_tile(0, 0, 0) == (0, 0, 0)
    assert tt.check_tile(22, 2**22 - 1, 2**22 - 1) == (22, 2**22 - 1, 2**22 - 1)
    assert tt.check_tile("10", "511", "340") == (10, 511, 340)
    with pytest.raises(ValueError):
        tt.check_tile("ten", 0, 0)
    for z in (10, 11, 12):
        for zoom, x, y in geo.tiles_for_box(
                type("B", (), {"west": -0.70, "south": 51.36, "east": -0.25, "north": 51.60})(), z):
            assert tt.check_tile(zoom, x, y) == (zoom, x, y)


def test_tags_list_is_the_documented_one():
    assert tt.TAGS == ["icon_category", "description", "road_type", "magnitude", "delay", "id",
                       "last_report_time", "number_of_reports", "probability_of_occurrence",
                       "road_category"]
    assert tt.TAGS_PARAM == "[" + ",".join(tt.TAGS) + "]"


# --- layers and classification ----------------------------------------------

def test_layer_kind_is_loose():
    assert tt.layer_kind("Traffic incident POI") == "poi"
    assert tt.layer_kind("Traffic incidents POI") == "poi"
    assert tt.layer_kind("traffic_incident_flow") == "flow"
    assert tt.layer_kind("Traffic incident flow") == "flow"
    assert tt.layer_kind("something else") == "other"
    assert tt.layer_kind("") == "other"


def test_layer_classification(features):
    assert len(features) == 5
    assert [f.layer for f in features].count("poi") == 4
    assert [f.layer for f in features].count("flow") == 1
    assert {f.layer_name for f in features} == {"Traffic incident POI", "Traffic incident flow"}
    assert all(f.tile == (ZOOM, X, Y) for f in features)
    assert all(f.extent == 4096 for f in features)


def test_breakdown_detection(features):
    tt1 = by_id(features, "tt-1")
    tt2 = by_id(features, "tt-2")
    tt3 = by_id(features, "tt-3")
    cluster = next(f for f in features if f.is_cluster)
    assert tt.is_breakdown(tt1)
    assert tt.is_breakdown(tt3)
    assert not tt.is_breakdown(tt2)
    assert not tt.is_breakdown(cluster)
    assert tt.breakdown_ids(features) == {"tt-1", "tt-3"}
    # the mixed cluster (13) MAY hide breakdowns; plain features never count as clusters
    assert tt.is_breakdown_cluster(cluster)
    assert not tt.is_breakdown_cluster(tt1)
    assert not tt.is_breakdown_cluster(tt3)


def _one_poi(props: dict, pixel=(1000, 1000)) -> tt.TileFeature:
    tile = mapbox_vector_tile.encode(
        [{"name": "Traffic incident POI",
          "features": [{"geometry": {"type": "Point", "coordinates": list(pixel)}, "properties": props}]}],
        default_options={"y_coord_down": True},
    )
    (f,) = tt.decode_tile(tile, ZOOM, X, Y)
    return f


def test_breakdown_cluster_flags_hidden_breakdowns():
    all_bd = _one_poi({"cluster_id": 1, "cluster_size": 2, "icon_category": 14, "clustered": 1})
    mixed = _one_poi({"cluster_id": 2, "cluster_size": 4, "icon_category": 13, "clustered": 1})
    accidents = _one_poi({"cluster_id": 3, "cluster_size": 2, "icon_category": 1, "clustered": 1})
    assert all_bd.is_cluster and mixed.is_cluster and accidents.is_cluster
    # a cluster has no id, so it is never a breakdown feature and never yields an id ...
    assert not tt.is_breakdown(all_bd) and not tt.is_breakdown(mixed)
    assert tt.breakdown_ids([all_bd, mixed, accidents]) == set()
    # ... but the module must flag that breakdowns are being hidden
    assert tt.is_breakdown_cluster(all_bd)
    assert tt.is_breakdown_cluster(mixed)
    assert not tt.is_breakdown_cluster(accidents)
    s = tt.summarize([all_bd, mixed, accidents])
    assert s["clusters"] == 3
    assert s["breakdown_clusters"] == 2
    assert s["breakdown_features"] == 0 and s["breakdown_ids"] == 0
    assert s["category_mentions"]["Broken Down Vehicle (in cluster)"] == 1


def test_feature_fields(features):
    tt1 = by_id(features, "tt-1")
    assert tt1.icon_categories == [14]
    assert tt1.descriptions == ["Broken down vehicle"]
    assert tt1.number_of_reports == 2
    assert tt1.poi_type == "start_poi"
    assert tt1.magnitude == 1 and tt1.delay == 0
    assert tt1.road_type == "Motorway"
    assert tt1.probability_of_occurrence == "certain"
    assert tt1.last_report_time == "2026-09-30T14:05:00Z"
    assert tt1.end_date is None
    assert tt1.geometry_type == "Point"
    assert tt1.pixel_coords == [(1000, 3000)]
    assert tt1.props["id"] == "tt-1"
    assert tt1.props is not None and isinstance(tt1.props, dict)

    tt3 = by_id(features, "tt-3")
    assert tt3.icon_categories == [6, 14]                      # index order kept
    assert tt3.descriptions == ["Stationary traffic", "Broken down vehicle"]

    cluster = next(f for f in features if f.is_cluster)
    assert cluster.cluster_id == 5 and cluster.cluster_size == 3
    assert cluster.icon_categories == [13]
    assert cluster.incident_id is None
    assert cluster.clustered == 1

    flow = by_id(features, "tt-1", layer="flow")
    assert flow.geometry_type == "LineString"
    assert len(flow.coords) >= 2
    assert flow.pixel_coords == [(1000, 3000), (1200, 3100), (1500, 3300)]
    assert flow.coord_parts == [flow.coords]


def test_every_coordinate_inside_tile_bounds(features):
    west, south, east, north = geo.tile_bounds(ZOOM, X, Y)
    eps = 1e-9
    for f in features:
        assert f.coords, f
        for lon, lat in f.coords:
            assert west - eps <= lon <= east + eps, (f.incident_id, lon)
            assert south - eps <= lat <= north + eps, (f.incident_id, lat)


def test_poi_pixel_matches_geo_formula(features):
    tt1 = by_id(features, "tt-1")
    assert tt1.coords == [geo.pixel_to_lonlat(ZOOM, X, Y, 1000, 3000)]
    # the POI and the first vertex of its flow line share a pixel, so the same lon/lat
    flow = by_id(features, "tt-1", layer="flow")
    assert flow.coords[0] == tt1.coords[0]


def test_dedupe_by_id_collapses_poi_and_flow(features):
    deduped = tt.dedupe_by_id(features)
    ids = [f.incident_id for f in deduped if f.incident_id]
    assert ids.count("tt-1") == 1
    assert len(deduped) == 4           # tt-1, tt-2, cluster (no id, kept), tt-3
    assert by_id(deduped, "tt-1").layer == "poi"   # first one wins
    assert sum(1 for f in deduped if f.is_cluster) == 1


# --- the y convention --------------------------------------------------------

def test_both_decoder_settings_give_the_same_lonlat(tile_bytes, features):
    """Hypothesis A either way: y_coord_down=False makes the library flip py to
    extent - py, and decode_tile undoes it."""
    flipped = tt.decode_tile(tile_bytes, ZOOM, X, Y, y_coord_down=False)
    assert len(flipped) == len(features)
    for a, b in zip(features, flipped):
        assert a.incident_id == b.incident_id and a.layer == b.layer
        assert a.pixel_coords == b.pixel_coords
        for (lon1, lat1), (lon2, lat2) in zip(a.coords, b.coords):
            assert abs(lon1 - lon2) < 1e-12
            assert abs(lat1 - lat2) < 1e-12


def test_mirrored_interpretation_differs_off_centre(tile_bytes, features):
    tt1 = by_id(features, "tt-1")                     # py = 3000, not the centre line
    m = tt.mirror_coords(feature=tt1)                 # the tile comes from feature.tile
    assert len(m) == 1
    assert m[0][0] == tt1.coords[0][0]                # longitude unchanged
    assert abs(m[0][1] - tt1.coords[0][1]) > 1e-4     # latitude moves
    assert tt.mirror_lat(feature=tt1) == m[0][1]
    # the mirrored point is what pixel (1000, 4096-3000) would be
    assert m[0] == geo.pixel_to_lonlat(ZOOM, X, Y, 1000, 4096 - 3000)
    # a point exactly on the horizontal centre line mirrors onto itself
    cluster = next(f for f in features if f.is_cluster)  # py = 2048
    assert abs(tt.mirror_lat(feature=cluster) - cluster.coords[0][1]) < 1e-12
    # the older four-argument form still works when it agrees with feature.tile ...
    assert tt.mirror_coords(ZOOM, X, Y, tt1) == m
    assert tt.mirror_coords(*tt1.tile, tt1) == m
    assert tt.mirror_lat(ZOOM, X, Y, tt1) == m[0][1]
    # ... and refuses a different tile instead of returning wrong latitudes
    with pytest.raises(ValueError):
        tt.mirror_coords(ZOOM, X, Y + 1, tt1)
    with pytest.raises(ValueError):
        tt.mirror_lat(ZOOM + 1, X, Y, tt1)
    with pytest.raises(ValueError):
        tt.mirror_coords(ZOOM, X, None, tt1)          # half a tile address
    with pytest.raises(ValueError):
        tt.mirror_coords()                            # no feature at all
    # raw_flip=True is the same hypothesis B as mirror_coords
    mirrored = tt.decode_tile(tile_bytes, ZOOM, X, Y, raw_flip=True)
    assert by_id(mirrored, "tt-1").coords == m
    assert by_id(mirrored, "tt-1").pixel_coords == tt1.pixel_coords   # stored ints unchanged


# --- summaries ----------------------------------------------------------------

def test_category_names():
    assert tt.category_name(14) == "Broken Down Vehicle"
    assert tt.category_name(1) == "Accident"
    assert tt.category_name(0) == "Unknown"
    assert tt.category_name(None) == "unknown"
    assert tt.category_name(99) == "unknown category 99"


def test_summarize(features):
    s = tt.summarize(features)
    assert s["features"] == 5
    assert s["by_layer"] == {"poi": 4, "flow": 1, "other": 0}
    assert s["clusters"] == 1
    assert s["cluster_members_total"] == 3
    assert s["breakdown_features"] == 3          # tt-1 poi, tt-1 flow, tt-3
    assert s["breakdown_clusters"] == 1          # the mixed (13) cluster may hide one
    assert s["breakdown_ids"] == 2
    assert s["unique_ids"] == 3
    assert s["features_without_id"] == 1
    assert s["category_mentions"]["Broken Down Vehicle"] == 3
    assert s["category_mentions"]["Accident"] == 1
    assert s["category_mentions"]["Jam"] == 1
    assert s["category_mentions"]["Mixed cluster (in cluster)"] == 1
    # tt-3 mentions two categories, so mentions add up to more than features
    assert sum(s["category_mentions"].values()) == s["features"] + 1
    assert "by_category" not in s
    assert s["by_geometry"] == {"Point": 4, "LineString": 1}
    assert sum(s["by_layer"].values()) == sum(s["by_geometry"].values()) == s["features"]


def test_multilinestring_and_string_tags_are_tolerated():
    tile = mapbox_vector_tile.encode(
        [{"name": "Traffic incident flow", "features": [{
            "geometry": {"type": "MultiLineString",
                         "coordinates": [[[100, 100], [200, 200]], [[300, 300], [400, 500]]]},
            "properties": {"icon_category_0": "14", "id": "tt-9", "magnitude": "2",
                           "number_of_reports": "n/a"},
        }]}],
        default_options={"y_coord_down": True},
    )
    (f,) = tt.decode_tile(tile, ZOOM, X, Y)
    assert f.geometry_type == "MultiLineString"
    assert len(f.coord_parts) == 2 and len(f.coords) == 4
    assert f.icon_categories == [14] and f.magnitude == 2
    assert f.number_of_reports is None
    assert tt.is_breakdown(f)


def test_empty_tile_decodes_to_nothing():
    tile = mapbox_vector_tile.encode([{"name": "Traffic incident POI", "features": []}],
                                     default_options={"y_coord_down": True})
    assert tt.decode_tile(tile, ZOOM, X, Y) == []
    assert tt.decode_tile(b"", ZOOM, X, Y) == []


@pytest.mark.parametrize("body", [
    b"<html><body>403 Forbidden</body></html>",
    b'{"error": "Too Many Requests for the supplied API Key"}',
])
def test_non_tile_bytes_raise_a_plain_value_error(body):
    with pytest.raises(ValueError) as info:
        tt.decode_tile(body, ZOOM, X, Y)
    msg = str(info.value)
    assert "not a TomTom vector tile" in msg
    assert f"{len(body)} bytes" in msg
    assert repr(body[:16]) in msg
    assert info.value.__cause__ is not None        # the library's error is kept for debugging


def test_truncated_tile_raises_a_plain_value_error(tile_bytes):
    with pytest.raises(ValueError):
        tt.decode_tile(tile_bytes[: len(tile_bytes) // 2] + b"\xff\xff\xff", ZOOM, X, Y)
    with pytest.raises(ValueError):
        tt.decode_tile("not bytes at all", ZOOM, X, Y)  # type: ignore[arg-type]
