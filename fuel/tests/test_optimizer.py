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
    def test_gap_after_a_full_tank_start_still_raises(self):
        # No station near the start: begins with a full tank (500 mi), but 1200 mi needs more.
        with self.assertRaisesMessage(InfeasibleRouteError, "stretch without a known fuel station"):
            plan([(60, 3.0)], route_miles=1200)

    def test_gap_longer_than_range(self):
        with self.assertRaisesMessage(InfeasibleRouteError, "520-mile stretch"):
            plan([(0, 3.0), (520, 3.0)], route_miles=900)

    def test_destination_out_of_range_of_last_station(self):
        with self.assertRaisesMessage(InfeasibleRouteError, "stretch without a known fuel station"):
            plan([(0, 3.0), (400, 3.0)], route_miles=1000)


class FullTankStartTests(SimpleTestCase):
    """No station near the start (e.g. Houston, Los Angeles): start with a full, unpaid tank."""

    def test_short_trip_needs_no_purchase(self):
        result = plan([(60, 3.0)], route_miles=300)
        self.assertEqual(result.purchases, [])
        self.assertEqual(result.starting_fuel_gallons, TANK)

    def test_long_trip_buys_only_what_the_full_tank_does_not_cover(self):
        for penalty in (0, 5):
            result = plan([(60, 3.0), (450, 2.5)], route_miles=700, stop_penalty=penalty)
            self.assertAlmostEqual(result.total_gallons, (700 - 500) / MPG)  # 20 gal on top of the full tank
            self.assertEqual(result.starting_fuel_gallons, TANK)
            self.assertEqual(result.purchases[-1].station.price, 2.5)  # bought at the cheaper station

    def test_full_tank_start_matches_lp(self):
        # Equivalent LP: a free station at mile 0 selling up to one full tank.
        rng = random.Random(11)
        for _ in range(100):
            route = rng.uniform(600, 2000)
            miles = [rng.uniform(30, 450)]
            while miles[-1] < route:
                miles.append(miles[-1] + rng.uniform(1, 480))
            stations = [(m, round(rng.uniform(2.6, 4.6), 3)) for m in miles if m <= route]
            result = plan(stations, route, start_radius=25)
            expected = lp_optimum([(0.0, 0.0)] + stations, route, start_index=0)
            self.assertAlmostEqual(result.total_cost, expected, places=6)


class DetourTests(SimpleTestCase):
    def test_detour_fuel_is_bought_and_costed(self):
        result = plan_fuel_stops(
            [StationOption(0, 3.0, detour_miles=10)], route_miles=200, range_miles=RANGE, mpg=MPG,
            start_radius_miles=25,
        )
        purchase = result.purchases[0]
        # First fill-up: the 5 mi out to the pump is lead-in, the 5 mi back is the detour.
        self.assertAlmostEqual(purchase.lead_in_gallons, 0.5)
        self.assertAlmostEqual(purchase.detour_gallons, 0.5)
        self.assertAlmostEqual(purchase.gallons, 21.0)
        self.assertAlmostEqual(purchase.cost, 63.0)

    def test_must_arrive_with_fuel_to_reach_an_off_route_pump(self):
        # On a full tank from mile 0 the vehicle reaches mile 500 empty, so it can't leave
        # the route for the 5-mile drive to the cheap pump there; it stops at mile 480 first.
        stations = [StationOption(0, 3.0), StationOption(480, 3.5), StationOption(500, 2.0, detour_miles=10)]
        result = plan_fuel_stops(stations, route_miles=900, range_miles=RANGE, mpg=MPG, start_radius_miles=25)
        cheap = result.purchases[-1]
        self.assertEqual(cheap.station.mile_marker, 500)
        self.assertAlmostEqual(cheap.fuel_on_arrival_gallons, 0.0)  # at the pump, after the 5 mi out
        self.assertAlmostEqual(result.total_gallons, 90 + 1)  # 900 mi plus one 10 mi round trip
        self.assertIn(480, [p.station.mile_marker for p in result.purchases])

    def test_tank_capacity_holds_with_detours(self):
        rng = random.Random(5)
        for penalty in (0, 5):
            for _ in range(150):
                route = rng.uniform(300, 2500)
                miles = [rng.uniform(0, 20)]
                while miles[-1] < route:
                    miles.append(miles[-1] + rng.uniform(1, 300))
                stations = [
                    StationOption(m, round(rng.uniform(2.6, 4.6), 3), detour_miles=rng.choice([0, rng.uniform(0, 20)]))
                    for m in miles
                    if m <= route
                ]
                try:
                    result = plan_fuel_stops(
                        stations, route_miles=route, range_miles=RANGE, mpg=MPG, start_radius_miles=25,
                        stop_penalty=penalty,
                    )
                except InfeasibleRouteError:
                    continue
                for p in result.purchases:
                    self.assertGreaterEqual(p.fuel_on_arrival_gallons, 0.0)
                    in_tank = p.fuel_on_arrival_gallons + p.gallons - p.lead_in_gallons
                    self.assertLessEqual(in_tank, TANK + 1e-6)
                detours = sum(p.station.detour_miles for p in result.purchases)
                self.assertAlmostEqual(result.total_gallons, (route + detours) / MPG, places=6)

    def test_far_off_route_station_loses_to_a_slightly_pricier_on_route_one(self):
        # 18 mi round trip at $3.00 = $5.40 of detour fuel, more than the 1 cent/gal saved on 40 gal.
        result = plan_fuel_stops(
            [StationOption(0, 3.10), StationOption(100, 3.01), StationOption(101, 3.00, detour_miles=18)],
            route_miles=500, range_miles=RANGE, mpg=MPG, start_radius_miles=25,
        )
        self.assertNotIn(3.00, [p.station.price for p in result.purchases])


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
            # Every subset keeps the start station, so all candidates describe the same trip
            # (without it the planner would start with a free full tank).
            for mask in range(1, 1 << len(stations), 2):
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
