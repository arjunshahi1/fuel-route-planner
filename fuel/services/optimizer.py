"""Cheapest fuel plan along a fixed route.

Trip model (documented in the README and in every API response):

* The vehicle starts with an empty tank and fills up at a station near the
  start: one within ``start_radius_miles`` along the route. The few miles
  driven to reach it ("lead-in") are paid for at that station's price, so the
  total fuel bought always equals ``route_miles / mpg``.
* The vehicle arrives at the destination with an empty tank.

Two algorithms work together:

1. ``_greedy`` - the classic "gas station problem" greedy, which gives the
   exact minimum fuel cost when fuel can be bought in any quantity:

   * if a cheaper station is reachable on a full tank, buy just enough to
     reach the first such station;
   * otherwise, if the destination is reachable, buy just enough to finish;
   * otherwise fill the tank and continue to the cheapest station in range.

   Minimum cost alone produces impractical plans, e.g. stopping for 0.2 gallons
   because the next station is half a cent cheaper.

2. ``_select_stops`` - a dynamic programme over (station, fuel level) that
   minimises ``fuel cost + stop_penalty x number of stops``. It decides
   *where* to stop; the greedy then computes the exact gallons using only
   those stations. With ``stop_penalty=0`` the result equals the pure greedy.

Distances are in miles along the route. Fuel in the tank is tracked as
"miles of range" to avoid repeated MPG conversions.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

import numpy as np

EPSILON = 1e-9
DP_RESOLUTION_MILES = 0.1


@dataclass(frozen=True)
class StationOption:
    mile_marker: float
    price: float  # USD per gallon
    ref: Any = None  # caller's handle back to the station


@dataclass(frozen=True)
class Purchase:
    station: StationOption
    gallons: float  # total gallons paid for here (includes lead_in_gallons)
    cost: float
    fuel_on_arrival_gallons: float
    lead_in_gallons: float = 0.0  # first stop only: fuel used to drive from the start to here


@dataclass
class FuelPlan:
    purchases: list[Purchase] = field(default_factory=list)

    @property
    def total_gallons(self) -> float:
        return sum(p.gallons for p in self.purchases)

    @property
    def total_cost(self) -> float:
        return sum(p.cost for p in self.purchases)


class InfeasibleRouteError(Exception):
    """The route cannot be completed with the given range and station coverage."""


def plan_fuel_stops(
    stations: list[StationOption],
    route_miles: float,
    range_miles: float,
    mpg: float,
    start_radius_miles: float,
    stop_penalty: float = 0.0,
) -> FuelPlan:
    if route_miles < 0 or range_miles <= 0 or mpg <= 0 or stop_penalty < 0:
        raise ValueError("route_miles and stop_penalty must be >= 0; range_miles and mpg must be > 0")
    if route_miles <= EPSILON:
        return FuelPlan()

    stops = sorted(
        (s for s in stations if -EPSILON <= s.mile_marker <= route_miles + EPSILON),
        key=lambda s: (s.mile_marker, s.price),
    )
    start_limit = min(start_radius_miles, route_miles)
    if not any(s.mile_marker <= start_limit + EPSILON for s in stops):
        raise InfeasibleRouteError(
            f"No fuel station within {start_limit:g} miles of the start along the route, "
            "so the first fill-up cannot be made."
        )

    if stop_penalty <= 0:
        return _greedy(stops, route_miles, range_miles, mpg, start_limit)

    chosen = _select_stops(stops, route_miles, range_miles, mpg, start_limit, stop_penalty)
    if chosen is None:
        # No feasible plan: let the greedy produce a precise error message.
        return _greedy(stops, route_miles, range_miles, mpg, start_limit)
    try:
        return _greedy([stops[i] for i in chosen], route_miles, range_miles, mpg, start_limit)
    except InfeasibleRouteError:
        # Only possible through rounding at DP_RESOLUTION_MILES; fall back to the exact optimum.
        return _greedy(stops, route_miles, range_miles, mpg, start_limit)


def _greedy(stops: list[StationOption], route_miles, range_miles, mpg, start_limit) -> FuelPlan:
    start_options = [i for i, s in enumerate(stops) if s.mile_marker <= start_limit + EPSILON]
    if not start_options:
        raise InfeasibleRouteError("No fuel station near the start of the route.")
    # Cheapest station near the start (ties: the farthest one). Starting at the
    # cheapest is never worse: any plan starting elsewhere nearby can be matched
    # at equal or lower cost by buying here and topping up later.
    current = min(start_options, key=lambda i: (stops[i].price, -stops[i].mile_marker))

    miles = [s.mile_marker for s in stops]
    purchases: list[Purchase] = []
    fuel = 0.0  # range in the tank on arrival at `current`, in miles
    lead_in = stops[current].mile_marker  # miles driven before the first fill-up

    while True:
        here = stops[current]
        reach = here.mile_marker + range_miles
        in_range = range(current + 1, bisect_right(miles, reach + EPSILON))

        cheaper = next((j for j in in_range if stops[j].price < here.price - EPSILON), None)
        if cheaper is not None:
            target_miles, next_stop = stops[cheaper].mile_marker - here.mile_marker, cheaper
        elif route_miles <= reach + EPSILON:
            target_miles, next_stop = route_miles - here.mile_marker, None
        else:
            if not in_range:
                ahead = [s.mile_marker for s in stops[current + 1 :] if s.mile_marker > here.mile_marker]
                gap = (ahead[0] if ahead else route_miles) - here.mile_marker
                raise InfeasibleRouteError(
                    f"The route has a {gap:.0f}-mile stretch without a known fuel station after mile "
                    f"{here.mile_marker:.0f}, which exceeds the vehicle's {range_miles:g}-mile range."
                )
            next_stop = min(in_range, key=lambda j: (stops[j].price, -stops[j].mile_marker))
            target_miles = range_miles  # fill the tank

        buy_miles = max(0.0, target_miles - fuel) + lead_in
        if buy_miles > EPSILON:
            gallons = buy_miles / mpg
            purchases.append(
                Purchase(
                    station=here,
                    gallons=gallons,
                    cost=gallons * here.price,
                    fuel_on_arrival_gallons=fuel / mpg,
                    lead_in_gallons=lead_in / mpg,
                )
            )
        lead_in = 0.0
        fuel = max(fuel, target_miles)

        if next_stop is None:
            return FuelPlan(purchases)
        fuel = max(fuel - (stops[next_stop].mile_marker - here.mile_marker), 0.0)
        current = next_stop


def _select_stops(
    stops: list[StationOption], route_miles, range_miles, mpg, start_limit, stop_penalty
) -> list[int] | None:
    """Indices of the stations to stop at, minimising fuel cost + stop_penalty per stop.

    ``cost[f]`` is the cheapest way to be at the current station holding ``f``
    units of fuel (1 unit = DP_RESOLUTION_MILES of range). Per station:

    * arrive: shift the previous station's array by the distance driven;
    * start: a station near the start may begin the trip with an empty tank
      (paying for the lead-in miles at its price);
    * buy: ``cost'[g] = min(cost[g], min_{f<g} cost[f] + (g - f) * price + penalty)``,
      a prefix minimum, so each station costs O(tank size) with numpy.

    Returns None when no feasible plan exists.
    """
    res = DP_RESOLUTION_MILES
    size = int(round(range_miles / res)) + 1
    levels = np.arange(size)
    positions = [int(round(s.mile_marker / res)) for s in stops]
    infinity = np.full(size, np.inf)

    cost = infinity.copy()
    bought, came_from, started_here = [], [], []
    for i, station in enumerate(stops):
        if i:
            drive = positions[i] - positions[i - 1]
            if drive:
                cost = np.concatenate((cost[drive:], infinity[: min(drive, size)]))[:size]

        start_cost = station.mile_marker * station.price / mpg
        starts = station.mile_marker <= start_limit + EPSILON and start_cost < cost[0]
        if starts:
            cost[0] = start_cost
        started_here.append(starts)

        unit_price = station.price * res / mpg
        shifted = cost - levels * unit_price
        running_min = np.minimum.accumulate(shifted)
        running_arg = np.maximum.accumulate(np.where(shifted <= running_min, levels, -1))
        best_before = np.concatenate(([np.inf], running_min[:-1]))  # min over f < g
        best_before_arg = np.concatenate(([-1], running_arg[:-1]))
        with_purchase = best_before + levels * unit_price + stop_penalty
        take = with_purchase < cost
        cost = np.where(take, with_purchase, cost)
        bought.append(take)
        came_from.append(np.where(take, best_before_arg, levels).astype(np.int32))

    needed = int(round(route_miles / res)) - positions[-1]
    if needed >= size:
        return None
    final = np.where(levels >= max(needed, 0), cost, np.inf)
    level = int(np.argmin(final))
    if not np.isfinite(final[level]):
        return None

    chosen = []
    for i in range(len(stops) - 1, -1, -1):
        if bought[i][level]:
            chosen.append(i)
            level = int(came_from[i][level])
        if level == 0 and started_here[i]:
            break  # the trip began at this station
        if i:
            level += positions[i] - positions[i - 1]
    return sorted(chosen)
