"""Cheapest fuel plan along a fixed route.

Trip model (documented in the README and in every API response):

* Normally the vehicle starts with an empty tank and fills up at a station
  near the start, one within ``start_radius_miles`` along the route. The miles
  driven to reach it ("lead-in") are paid for at that station's price.
* If no station is that close (the price data has none in some large cities),
  the vehicle starts with a full tank instead. That fuel is not part of the
  trip's cost; the plan reports it as ``starting_fuel_gallons``.
* The vehicle arrives at the destination with an empty tank.
* Stations lie up to a few miles off the route. The round trip off the route
  ("detour") burns fuel, which is bought at that station and added to its cost.
  The vehicle must reach the station with that fuel in the tank, and the tank
  must also hold the drive back, so a full tank leaves less range on the route.
  On the first fill-up the drive to the pump is part of the lead-in.

Two algorithms work together:

1. ``_greedy`` - the classic "gas station problem" greedy, which gives the
   exact minimum fuel cost when fuel can be bought in any quantity:

   * if a cheaper station is reachable on a full tank, buy just enough to
     reach the first such station;
   * otherwise, if the destination is reachable, buy just enough to finish;
   * otherwise fill the tank and continue to the cheapest station in range.

2. ``_select_stops`` - a dynamic programme over (station, fuel level) that
   minimises ``fuel cost + (stop_penalty + detour cost) per stop``. It decides
   *where* to stop; the greedy then computes the exact gallons using only
   those stations. A stop penalty avoids impractical plans such as stopping for
   0.2 gallons because the next station is half a cent cheaper.

Distances are in miles along the route. Fuel in the tank is tracked as
"miles of range" to avoid repeated MPG conversions.
"""

from __future__ import annotations

import math
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
    detour_miles: float = 0.0  # extra miles to leave the route, reach the station and return


@dataclass(frozen=True)
class Purchase:
    station: StationOption
    gallons: float  # total gallons paid for here (includes lead-in and detour fuel)
    cost: float
    fuel_on_arrival_gallons: float
    lead_in_gallons: float = 0.0  # first stop only: fuel used to drive from the start to here
    detour_gallons: float = 0.0  # fuel for the round trip off the route to this station


@dataclass
class FuelPlan:
    purchases: list[Purchase] = field(default_factory=list)
    starting_fuel_gallons: float = 0.0  # already in the tank at the start; not part of the cost

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
    start_full = not any(s.mile_marker <= start_limit + EPSILON for s in stops)

    def greedy(subset):
        return _greedy(subset, route_miles, range_miles, mpg, start_limit, start_full)

    if not stops or (stop_penalty <= 0 and all(s.detour_miles <= 0 for s in stops)):
        plan = greedy(stops)
    else:
        chosen = _select_stops(stops, route_miles, range_miles, mpg, start_limit, stop_penalty, start_full)
        if chosen is None:
            plan = greedy(stops)  # no feasible plan: the greedy raises a precise error
        else:
            try:
                plan = greedy([stops[i] for i in chosen])
            except InfeasibleRouteError:
                # Only possible through rounding at DP_RESOLUTION_MILES; fall back to all stations.
                plan = greedy(stops)

    if start_full:
        plan.starting_fuel_gallons = range_miles / mpg
    return plan


def _greedy(stops: list[StationOption], route_miles, range_miles, mpg, start_limit, start_full) -> FuelPlan:
    if start_full:
        # A virtual, unbuyable station at the origin holding a full tank.
        stops = [StationOption(0.0, math.inf)] + list(stops)
        current, fuel, at_start = 0, range_miles, False
    else:
        start_options = [i for i, s in enumerate(stops) if s.mile_marker <= start_limit + EPSILON]
        if not start_options:
            raise InfeasibleRouteError("No fuel station near the start of the route.")
        # Cheapest station near the start (ties: the farthest one). Starting at the
        # cheapest is never worse: any plan starting elsewhere nearby can be matched
        # at equal or lower cost by buying here and topping up later.
        current = min(start_options, key=lambda i: (stops[i].price, -stops[i].mile_marker))
        fuel = 0.0  # range in the tank on arrival at `current`'s route point, in miles
        at_start = True  # the first fill-up: the miles driven to reach it are the lead-in

    miles = [s.mile_marker for s in stops]
    purchases: list[Purchase] = []

    while True:
        here = stops[current]
        off = here.detour_miles / 2  # one way, between the route and the pump
        cap = range_miles - off  # most range left on rejoining the route after filling here
        reach = here.mile_marker + cap
        in_range = [
            j
            for j in range(current + 1, bisect_right(miles, reach + EPSILON))
            if stops[j].mile_marker - here.mile_marker + stops[j].detour_miles / 2 <= cap + EPSILON
        ]

        cheaper = next((j for j in in_range if stops[j].price < here.price - EPSILON), None)
        if cheaper is not None:
            # Arrive with enough fuel to drive off the route to its pump.
            target_miles = stops[cheaper].mile_marker - here.mile_marker + stops[cheaper].detour_miles / 2
            next_stop = cheaper
        elif route_miles <= reach + EPSILON:
            target_miles, next_stop = route_miles - here.mile_marker, None
        else:
            if not in_range:
                ahead = [s.mile_marker for s in stops[current + 1:] if s.mile_marker > here.mile_marker]
                gap = (ahead[0] if ahead else route_miles) - here.mile_marker
                raise InfeasibleRouteError(
                    f"The route has a {gap:.0f}-mile stretch without a known fuel station after mile "
                    f"{here.mile_marker:.0f}, which exceeds the vehicle's {range_miles:g}-mile range."
                )
            next_stop = min(in_range, key=lambda j: (stops[j].price, -stops[j].mile_marker))
            target_miles = cap  # fill the tank

        if at_start:
            lead_in, detour, at_pump = here.mile_marker + off, off, 0.0
        else:
            lead_in, detour, at_pump = 0.0, 2 * off, max(fuel - off, 0.0)
        top_up = max(0.0, target_miles - fuel)
        if top_up + lead_in > EPSILON:  # no purchase means no detour
            gallons = (top_up + lead_in + detour) / mpg
            purchases.append(
                Purchase(
                    station=here,
                    gallons=gallons,
                    cost=gallons * here.price,
                    fuel_on_arrival_gallons=at_pump / mpg,
                    lead_in_gallons=lead_in / mpg,
                    detour_gallons=detour / mpg,
                )
            )
        at_start = False
        fuel = max(fuel, target_miles)

        if next_stop is None:
            return FuelPlan(purchases)
        fuel = max(fuel - (stops[next_stop].mile_marker - here.mile_marker), 0.0)
        current = next_stop


def _select_stops(
    stops: list[StationOption], route_miles, range_miles, mpg, start_limit, stop_penalty, start_full
) -> list[int] | None:
    """Indices of the stations to stop at, minimising fuel cost + per-stop costs.

    ``cost[f]`` is the cheapest way to be at the current station holding ``f``
    units of fuel (1 unit = DP_RESOLUTION_MILES of range). Per station:

    * arrive: shift the previous array by the distance driven;
    * start: either the trip begins at the origin with a full tank, or a
      station near the start may begin it with an empty tank (paying for the
      lead-in miles at its price);
    * buy: ``cost'[g] = min(cost[g], min_{f<g} cost[f] + (g - f) * price + stop_cost)``,
      a prefix minimum, so each station costs O(tank size) with numpy. The stop
      cost is ``stop_penalty`` plus the fuel for the station's detour.

    Returns None when no feasible plan exists.
    """
    res = DP_RESOLUTION_MILES
    size = int(round(range_miles / res)) + 1
    levels = np.arange(size)
    positions = [int(round(s.mile_marker / res)) for s in stops]
    infinity = np.full(size, np.inf)

    cost = infinity.copy()
    if start_full:
        cost[-1] = 0.0  # full tank at the origin, already paid for
    previous_position = 0
    bought, came_from, started_here = [], [], []
    for i, station in enumerate(stops):
        drive = positions[i] - previous_position
        if drive:
            cost = np.concatenate((cost[drive:], infinity[: min(drive, size)]))[:size]
        previous_position = positions[i]

        start_cost = station.mile_marker * station.price / mpg
        starts = not start_full and station.mile_marker <= start_limit + EPSILON and start_cost < cost[0]
        if starts:
            cost[0] = start_cost
        started_here.append(starts)

        unit_price = station.price * res / mpg
        stop_cost = stop_penalty + station.detour_miles * station.price / mpg
        off_units = math.ceil(station.detour_miles / 2 / res - EPSILON)  # one way to the pump
        shifted = cost - levels * unit_price
        if off_units > 0:
            # Buying needs the fuel to reach the pump, except on the first fill-up (its lead-in covers it).
            blocked = levels < off_units
            blocked[0] = blocked[0] and not starts
            shifted = np.where(blocked, np.inf, shifted)
        running_min = np.minimum.accumulate(shifted)
        running_arg = np.maximum.accumulate(np.where(shifted <= running_min, levels, -1))
        best_before = np.concatenate(([np.inf], running_min[:-1]))  # min over f < g
        best_before_arg = np.concatenate(([-1], running_arg[:-1]))
        with_purchase = best_before + levels * unit_price + stop_cost
        if off_units > 0:
            with_purchase[size - off_units:] = np.inf  # the tank must also hold the drive back to the route
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
