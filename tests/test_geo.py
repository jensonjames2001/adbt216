"""Tests for rgalerts.geo (pure maths, no I/O)."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from rgalerts import geo
from rgalerts.config import Box, load_config

REPO = Path(__file__).resolve().parents[1]

# The brief's box, written out literally on purpose: the owner is told to edit
# config.yaml, and the tile counts below are facts about THIS box, not about
# whatever the owner has typed in. Only test_config_box_loads_and_is_not_inverted
# reads config.yaml, and it asserts shape, never numbers.
BRIEF_BOX = Box(west=-0.70, south=51.36, east=-0.25, north=51.60)


def default_box() -> Box:
    return BRIEF_BOX


def test_config_box_loads_and_is_not_inverted():
    box = load_config(REPO / "config.yaml")["box_obj"]
    assert isinstance(box, Box)
    assert box.west < box.east and box.south < box.north
    assert -180.0 <= box.west and box.east <= 180.0
    assert -90.0 <= box.south and box.north <= 90.0
    # whatever the owner's box is, tiles_for_box must cover it without complaint
    assert geo.tiles_for_box(box, 10)


# --- pixel maths -----------------------------------------------------------

def test_world_tile_centre_is_origin():
    lon, lat = geo.pixel_to_lonlat(0, 0, 0, 2048, 2048)
    assert abs(lon) < 1e-9
    assert abs(lat) < 1e-9


def test_tile_10_511_340_top_left_pixel():
    lon, lat = geo.pixel_to_lonlat(10, 511, 340, 0, 0)
    assert lon == -0.3515625
    # independent computation from the slippy-map formula
    n = 2 ** 10
    expected_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * 340 / n))))
    assert abs(lat - expected_lat) < 0.001
    assert 51.6 < lat < 51.64  # "about 51.618"


def test_pixel_outside_extent_is_not_clamped():
    # buffer pixels just outside the tile must convert past the tile edge
    west, south, east, north = geo.tile_bounds(10, 511, 340)
    lon, lat = geo.pixel_to_lonlat(10, 511, 340, -64, -64)
    assert lon < west
    assert lat > north
    lon2, lat2 = geo.pixel_to_lonlat(10, 511, 340, 4096 + 64, 4096 + 64)
    assert lon2 > east
    assert lat2 < south


def test_pixel_corners_match_tile_bounds():
    west, south, east, north = geo.tile_bounds(11, 1021, 681)
    lon, lat = geo.pixel_to_lonlat(11, 1021, 681, 0, 0)
    assert abs(lon - west) < 1e-12 and abs(lat - north) < 1e-12
    lon, lat = geo.pixel_to_lonlat(11, 1021, 681, 4096, 4096)
    assert abs(lon - east) < 1e-12 and abs(lat - south) < 1e-12


# --- tiles for the box -------------------------------------------------------

def test_tiles_for_default_box_zoom_10():
    tiles = geo.tiles_for_box(default_box(), 10)
    assert set(tiles) == {(10, 510, 340), (10, 511, 340), (10, 510, 341), (10, 511, 341)}
    assert tiles == sorted(tiles)
    assert len(tiles) == len(set(tiles))


@pytest.mark.parametrize("zoom,expected", [(10, 4), (11, 9), (12, 30)])
def test_tiles_for_default_box_counts(zoom, expected):
    tiles = geo.tiles_for_box(default_box(), zoom)
    xs = sorted({t[1] for t in tiles})
    ys = sorted({t[2] for t in tiles})
    assert len(tiles) == expected, (
        f"zoom {zoom}: computed {len(tiles)} tiles (x {xs[0]}..{xs[-1]}, y {ys[0]}..{ys[-1]}), "
        f"expected {expected}"
    )
    assert all(t[0] == zoom for t in tiles)


def test_tiles_for_box_accepts_any_object_with_edges():
    class B:
        west, south, east, north = -0.70, 51.36, -0.25, 51.60
    assert geo.tiles_for_box(B(), 10) == geo.tiles_for_box(default_box(), 10)


def test_tiles_for_box_rejects_inverted_box():
    with pytest.raises(ValueError):
        geo.tiles_for_box(Box(west=-0.25, south=51.36, east=-0.70, north=51.60), 10)


def test_tiles_cover_every_box_corner():
    box = default_box()
    for zoom in (10, 11, 12):
        tiles = set(geo.tiles_for_box(box, zoom))
        for lon, lat in [(box.west, box.north), (box.east, box.north),
                         (box.west, box.south), (box.east, box.south),
                         (-0.4094, 51.4489)]:
            x, y = geo.lonlat_to_tile(lon, lat, zoom)
            assert (zoom, x, y) in tiles


# --- tile <-> bounds round trip ----------------------------------------------

@pytest.mark.parametrize("lon,lat", [
    (-0.4094, 51.4489),   # Feltham
    (-0.4879, 51.4723),   # Heathrow T5
    (-0.70, 51.36), (-0.25, 51.60),
    (0.0, 0.0), (139.69, 35.68), (-73.99, 40.73), (151.2, -33.87),
])
@pytest.mark.parametrize("zoom", [0, 5, 10, 12, 18])
def test_lonlat_to_tile_round_trips_with_tile_bounds(lon, lat, zoom):
    x, y = geo.lonlat_to_tile(lon, lat, zoom)
    n = 2 ** zoom
    assert 0 <= x < n and 0 <= y < n
    west, south, east, north = geo.tile_bounds(zoom, x, y)
    assert west <= lon <= east
    assert south <= lat <= north
    # the tile's own north-west corner maps back to the same tile
    assert geo.lonlat_to_tile(west + 1e-9, north - 1e-9, zoom) == (x, y)


def test_lonlat_to_tile_clamps_edges():
    assert geo.lonlat_to_tile(180.0, 0.0, 3) == (7, 4)   # lon 180 clamps to the last column; equator is the row 3/4 edge
    assert geo.lonlat_to_tile(-180.0, 89.0, 3) == (0, 0)
    assert geo.lonlat_to_tile(0.0, -89.0, 3) == (4, 7)


# --- distances ---------------------------------------------------------------

def test_haversine_feltham_to_heathrow_t5():
    d = geo.haversine_m(51.4489, -0.4094, 51.4723, -0.4879)
    assert 5900 <= d <= 6300
    assert 3.6 < geo.miles(d) < 4.0


def test_haversine_zero_and_symmetry():
    assert geo.haversine_m(51.5, -0.4, 51.5, -0.4) == 0.0
    a = geo.haversine_m(51.4489, -0.4094, 51.4723, -0.4879)
    b = geo.haversine_m(51.4723, -0.4879, 51.4489, -0.4094)
    assert abs(a - b) < 1e-6


def test_miles():
    assert abs(geo.miles(1609.344) - 1.0) < 1e-12
    assert abs(geo.miles(10000) - 6.2137) < 0.001


def test_bearing_cardinals():
    assert abs(geo.bearing_deg(51.0, 0.0, 52.0, 0.0) - 0.0) < 1e-9        # north
    assert abs(geo.bearing_deg(0.0, 0.0, 0.0, 1.0) - 90.0) < 1e-9         # east
    assert abs(geo.bearing_deg(52.0, 0.0, 51.0, 0.0) - 180.0) < 1e-9      # south
    assert abs(geo.bearing_deg(0.0, 0.0, 0.0, -1.0) - 270.0) < 1e-9       # west
    b = geo.bearing_deg(51.4489, -0.4094, 51.4723, -0.4879)               # Feltham -> T5: north-west
    assert 290 < b < 320
    assert 0.0 <= geo.bearing_deg(1.0, 1.0, 1.0, 1.0) < 360.0


# --- point to polyline -------------------------------------------------------

def test_point_to_polyline_100m_north_of_west_east_segment():
    lat0, lon0 = 51.45, -0.40
    dlat = 100.0 / geo.EARTH_RADIUS_M * 180.0 / math.pi     # 100 m in degrees of latitude
    line = [(lon0 - 0.01, lat0), (lon0 + 0.01, lat0)]        # west -> east through lat0
    d = geo.point_to_polyline_m(lat0 + dlat, lon0, line)
    assert abs(d - 100.0) <= 2.0


def test_point_to_polyline_beyond_segment_end_uses_endpoint():
    lat0, lon0 = 51.45, -0.40
    line = [(lon0, lat0), (lon0 + 0.01, lat0)]
    # a point due west of the segment's western end, 200 m away
    dlon = 200.0 / (geo.EARTH_RADIUS_M * math.cos(math.radians(lat0))) * 180.0 / math.pi
    d = geo.point_to_polyline_m(lat0, lon0 - dlon, line)
    assert abs(d - 200.0) <= 2.0


def test_point_to_polyline_multi_segment_picks_nearest():
    line = [(-0.50, 51.40), (-0.45, 51.40), (-0.45, 51.45), (-0.40, 51.45)]
    d_on = geo.point_to_polyline_m(51.45, -0.42, line)
    assert d_on < 1.0
    d_off = geo.point_to_polyline_m(51.43, -0.44, line)
    assert 500 < d_off < 1500


def test_point_to_polyline_degenerate_lines():
    assert geo.point_to_polyline_m(51.45, -0.40, []) == math.inf
    single = geo.point_to_polyline_m(51.45, -0.40, [(-0.40, 51.45)])
    assert single == 0.0
    d = geo.point_to_polyline_m(51.4723, -0.4879, [(-0.4094, 51.4489)])
    assert abs(d - geo.haversine_m(51.4723, -0.4879, 51.4489, -0.4094)) < 20


# --- boxes -------------------------------------------------------------------

def test_point_in_box():
    box = Box(west=-0.70, south=51.36, east=-0.25, north=51.60)
    assert geo.point_in_box(51.4489, -0.4094, box)
    assert geo.point_in_box(51.36, -0.70, box)          # inclusive edge
    assert not geo.point_in_box(51.70, -0.40, box)
    assert not geo.point_in_box(51.45, -0.10, box)
    assert geo.point_in_box(51.4489, -0.4094, box) == box.contains(51.4489, -0.4094)
