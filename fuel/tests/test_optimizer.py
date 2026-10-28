import random

import numpy as np
from django.test import SimpleTestCase
from scipy.optimize import linprog

from fuel.services.optimizer import InfeasibleRouteError, StationOption, plan_fuel_stops

RANGE, MPG = 500.0, 10.0
TANK = RANGE / MPG


def plan(stations, route_miles, start_radius=25.0, stop_penalty=0.0):
    return plan_fuel_stops(
        [StationOption(m, p) for m, p in stations],
        route_miles=route_miles,
        range_miles=RANGE,
        mpg=MPG,
        start_radius_miles=start_radius,
        stop_penalty=stop_penalty,
    )


def lp_optimum(stations, route_miles, start_index):
    """Independent reference: minimum cost via linear programming.

    Variables are gallons bought at each station from ``start_index`` on. The
    tank must never run dry between stations or hold more than TANK gallons.
    Miles driven before the start station are bought there (the API's model).
    """
    stations = sorted(stations)[start_index:]
    n = len(stations)
    prices = [p for _, p in stations]
    A_ub, b_ub = [], []
    for k in range(n):
        # Gallons burned from mile 0 (trip origin) to station k, lead-in included.
        burned = stations[k][0] / MPG
        if k > 0:  # arrive at k with >= 0 fuel: sum_{i<k} x_i >= burned
            A_ub.append([-1.0 if i < k else 0.0 for i in range(n)])
            b_ub.append(-burned)
        # leave k with <= TANK in the tank, ignoring the lead-in already burned
        # before the first station: sum_{i<=k} x_i - burned <= TANK
        A_ub.append([1.0 if i <= k else 0.0 for i in range(n)])
        b_ub.append(burned + TANK)
    A_ub.append([-1.0] * n)  # reach the destination
    b_ub.append(-route_miles / MPG)
    res = linprog(prices, A_ub=np.array(A_ub), b_ub=np.array(b_ub), bounds=[(0, None)] * n, method="highs")
    return res.fun if res.success else None


class OptimizerBasicsTests(SimpleTestCase):
    def test_short_trip_buys_exactly_the_distance_at_the_start(self):
        result = plan([(0, 3.00), (100, 3.50)], route_miles=300)
        self.assertEqual(len(result.purchases), 1)
        self.assertAlmostEqual(result.total_gallons, 30.0)
        self.assertAlmostEqual(result.total_cost, 90.0)

    def test_buys_just_enough_to_reach_a_cheaper_station(self):
        result = plan([(0, 4.00), (100, 3.00)], route_miles=400)
        gallons = [round(p.gallons, 6) for p in result.purchases]
        self.assertEqual(gallons, [10.0, 30.0])
        self.assertAlmostEqual(result.total_cost, 10 * 4.0 + 30 * 3.0)

    def test_fills_up_when_no_cheaper_station_is_in_range(self):
        # Cheap start, pricier stations later: fill the tank at the start.
        result = plan([(0, 3.00), (450, 3.50), (700, 3.60)], route_miles=900)
        first = result.purchases[0]
        self.assertAlmostEqual(first.gallons, TANK)
        self.assertAlmostEqual(result.total_gallons, 90.0)

    def test_multiple_stops_on_a_long_route(self):
        stations = [(0, 3.2), (300, 3.1), (450, 3.5), (800, 2.9), (1100, 3.4), (1300, 3.0)]
        result = plan(stations, route_miles=1600)
        self.assertAlmostEqual(result.total_gallons, 160.0)
        for p in result.purchases:  # tank capacity is never exceeded
            self.assertLessEqual(p.fuel_on_arrival_gallons + p.gallons, TANK + 1e-9)

    def test_total_gallons_always_equal_distance_over_mpg(self):
        result = plan([(10, 3.0), (400, 3.2), (700, 2.8)], route_miles=1000)
        self.assertAlmostEqual(result.total_gallons, 100.0)

    def test_lead_in_miles_are_charged_at_the_first_station(self):
        result = plan([(20, 3.0)], route_miles=200)
        self.assertAlmostEqual(result.total_gallons, 20.0)
        self.assertAlmostEqual(result.total_cost, 60.0)

    def test_starts_at_the_cheapest_station_near_the_start(self):
        result = plan([(2, 3.80), (15, 3.10), (40, 2.50)], route_miles=300)
        self.assertEqual(result.purchases[0].station.mile_marker, 15)

    def test_zero_distance_needs_no_fuel(self):
        self.assertEqual(plan([(0, 3.0)], route_miles=0).purchases, [])

    def test_stations_beyond_the_destination_are_ignored(self):
        result = plan([(0, 3.5), (350, 1.0)], route_miles=300)
        self.assertEqual(len(result.purchases), 1)
        self.assertAlmostEqual(result.total_cost, 30 * 3.5)


class OptimizerInfeasibleTests(SimpleTestCase):
    def test_no_station_near_the_start(self):
        with self.assertRaisesMessage(InfeasibleRouteError, "within 25 miles of the start"):
            plan([(60, 3.0)], route_miles=300)

    def test_gap_longer_than_range(self):
        with self.assertRaisesMessage(InfeasibleRouteError, "520-mile stretch"):
            plan([(0, 3.0), (520, 3.0)], route_miles=900)

    def test_destination_out_of_range_of_last_station(self):
        with self.assertRaisesMessage(InfeasibleRouteError, "stretch without a known fuel station"):
            plan([(0, 3.0), (400, 3.0)], route_miles=1000)


class StopPenaltyTests(SimpleTestCase):
    def test_penalty_removes_stops_that_save_almost_nothing(self):
        stations = [(0, 3.05), (2, 3.04), (200, 3.03), (300, 3.02)]
        exact = plan(stations, route_miles=400)
        practical = plan(stations, route_miles=400, stop_penalty=5)
        self.assertGreater(len(exact.purchases), 1)
        self.assertEqual(len(practical.purchases), 1)
        self.assertAlmostEqual(practical.total_gallons, 40.0)

    def test_still_stops_when_savings_exceed_the_penalty(self):
        # Waiting for the $3.00 station saves 30 gal x $0.50 = $15 > $5.
        result = plan([(0, 3.50), (100, 3.00)], route_miles=400, stop_penalty=5)
        self.assertEqual(len(result.purchases), 2)

    def test_skips_a_stop_when_savings_are_below_the_penalty(self):
        # Saves 30 gal x $0.10 = $3 < $5, so buy everything at the start.
        result = plan([(0, 3.10), (100, 3.00)], route_miles=400, stop_penalty=5)
        self.assertEqual(len(result.purchases), 1)

    def test_penalty_never_breaks_feasibility_or_totals(self):
        stations = [(0, 3.2), (300, 3.1), (450, 3.5), (800, 2.9), (1100, 3.4), (1300, 3.0)]
        result = plan(stations, route_miles=1600, stop_penalty=50)
        self.assertAlmostEqual(result.total_gallons, 160.0)
        self.assertGreaterEqual(len(result.purchases), 4)  # 1600 mi needs >= 4 fills with a 500 mi range

    def test_lead_in_is_reported_on_the_first_stop(self):
        result = plan([(20, 3.0), (300, 3.0)], route_miles=600, stop_penalty=5)
        self.assertAlmostEqual(result.purchases[0].lead_in_gallons, 2.0)
        self.assertLessEqual(result.purchases[0].gallons - result.purchases[0].lead_in_gallons, TANK + 1e-9)
        self.assertTrue(all(p.lead_in_gallons == 0 for p in result.purchases[1:]))

    def test_infeasible_routes_still_raise(self):
        with self.assertRaises(InfeasibleRouteError):
            plan([(0, 3.0), (520, 3.0)], route_miles=900, stop_penalty=5)

    def test_dp_finds_the_best_cost_plus_penalty_trade_off(self):
        """Brute force: every subset of stations, priced by the exact greedy."""
        rng = random.Random(3)
        checked = 0
        while checked < 60:
            route = rng.uniform(600, 1800)
            stations = [(0.0, round(rng.uniform(2.6, 4.6), 3))]
            while stations[-1][0] < route and len(stations) < 10:
                stations.append((stations[-1][0] + rng.uniform(5, 300), round(rng.uniform(2.6, 4.6), 3)))
            stations = [s for s in stations if s[0] <= route]
            try:
                plan(stations, route, start_radius=0)
            except InfeasibleRouteError:
                continue  # no plan exists for this random instance; draw another
            checked += 1
            penalty = rng.choice([1.0, 5.0, 20.0])

            best = float("inf")
            for mask in range(1, 1 << len(stations)):
                subset = [s for k, s in enumerate(stations) if mask >> k & 1]
                try:
                    candidate = plan(subset, route, start_radius=0)
                except InfeasibleRouteError:
                    continue
                best = min(best, candidate.total_cost + penalty * len(candidate.purchases))

            result = plan(stations, route, start_radius=0, stop_penalty=penalty)
            objective = result.total_cost + penalty * len(result.purchases)
            self.assertAlmostEqual(objective, best, delta=0.05)


class OptimizerMatchesLinearProgramTests(SimpleTestCase):
    """The greedy must equal the true optimum computed by an LP solver."""

    def _random_instance(self, rng):
        route = rng.uniform(100, 3000)
        miles = [0.0]
        while miles[-1] < route:
            miles.append(miles[-1] + rng.uniform(1, 480))
        miles = [m for m in miles if m <= route]
        return [(m, round(rng.uniform(2.6, 4.6), 3)) for m in miles], route

    def test_greedy_equals_lp_optimum_on_random_routes(self):
        rng = random.Random(42)
        checked = 0
        for _ in range(400):
            stations, route = self._random_instance(rng)
            result = plan(stations, route, start_radius=0)
            expected = lp_optimum(stations, route, start_index=0)
            self.assertIsNotNone(expected)
            self.assertAlmostEqual(result.total_cost, expected, places=6)
            self.assertAlmostEqual(result.total_gallons, route / MPG, places=6)
            checked += 1
        self.assertEqual(checked, 400)

    def test_choosing_the_cheapest_start_station_is_optimal(self):
        rng = random.Random(7)
        for _ in range(200):
            stations, route = self._random_instance(rng)
            # Several candidate start stations within the start radius.
            stations = sorted(stations + [(rng.uniform(0, 25), round(rng.uniform(2.6, 4.6), 3)) for _ in range(3)])
            result = plan(stations, route, start_radius=25)
            starts = [i for i, (m, _) in enumerate(stations) if m <= 25]
            costs = [c for i in starts if (c := lp_optimum(stations, route, i)) is not None]
            self.assertAlmostEqual(result.total_cost, min(costs), places=6)
