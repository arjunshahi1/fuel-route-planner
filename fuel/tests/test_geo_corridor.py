from decimal import Decimal

import numpy as np
from django.test import SimpleTestCase

from fuel.services.corridor import stations_along_route
from fuel.services.geo import densify, haversine_miles, to_unit_vectors
from fuel.services.stations import StationIndex


def make_index(stations):
    """stations: list of (lat, lng, price)."""
    lat = np.array([s[0] for s in stations], dtype=float)
    lng = np.array([s[1] for s in stations], dtype=float)
    return StationIndex(
        ids=np.arange(len(stations)),
        latitudes=lat,
        longitudes=lng,
        prices=np.array([s[2] for s in stations], dtype=float),
        unit_vectors=to_unit_vectors(lat, lng),
        records=tuple({"price": Decimal(str(s[2]))} for s in stations),
    )


class GeoTests(SimpleTestCase):
    def test_haversine_known_distance(self):
        # Chicago -> Denver great-circle distance is ~920 miles.
        self.assertAlmostEqual(float(haversine_miles(41.8781, -87.6298, 39.7392, -104.9903)), 920, delta=5)

    def test_densify_preserves_endpoints_and_length(self):
        lats, lons = np.array([40.0, 40.0, 41.0]), np.array([-100.0, -99.0, -99.0])
        d_lat, d_lon, cum = densify(lats, lons, max_step_miles=0.5)
        self.assertEqual((d_lat[0], d_lon[0]), (40.0, -100.0))
        self.assertEqual((d_lat[-1], d_lon[-1]), (41.0, -99.0))
        expected = haversine_miles(40, -100, 40, -99) + haversine_miles(40, -99, 41, -99)
        self.assertAlmostEqual(cum[-1], float(expected), places=6)
        steps = haversine_miles(d_lat[:-1], d_lon[:-1], d_lat[1:], d_lon[1:])
        self.assertLessEqual(float(steps.max()), 0.5 + 1e-9)
        self.assertTrue(np.all(np.diff(cum) > 0))


class CorridorTests(SimpleTestCase):
    # A straight east-west route along latitude 40 from -100 to -98 (~106 miles).
    route = [[-100.0, 40.0], [-99.0, 40.0], [-98.0, 40.0]]
    route_miles = float(haversine_miles(40, -100, 40, -98))

    def test_finds_stations_near_the_route_with_mile_markers(self):
        index = make_index(
            [
                (40.05, -99.0, 3.1),  # ~3.5 mi north of the route midpoint
                (40.00, -100.0, 3.2),  # at the start
                (41.00, -99.0, 2.0),  # ~69 mi away: outside the corridor
                (40.00, -95.0, 2.0),  # past the end, far away
            ]
        )
        found = stations_along_route(self.route, self.route_miles, index, corridor_miles=10)
        self.assertEqual([s.index for s in found], [1, 0])
        start, middle = found
        self.assertAlmostEqual(start.mile_marker, 0.0, delta=0.3)
        self.assertAlmostEqual(middle.mile_marker, self.route_miles / 2, delta=0.5)
        self.assertAlmostEqual(middle.off_route_miles, 3.45, delta=0.1)

    def test_mile_markers_are_scaled_to_the_road_distance(self):
        index = make_index([(40.0, -98.0, 3.0)])
        found = stations_along_route(self.route, 150.0, index, corridor_miles=5)
        self.assertAlmostEqual(found[0].mile_marker, 150.0, delta=0.5)

    def test_empty_inputs(self):
        self.assertEqual(stations_along_route(self.route, 100, make_index([]), 10), [])
        self.assertEqual(stations_along_route([[-100.0, 40.0]], 0, make_index([(40, -100, 3)]), 10), [])
