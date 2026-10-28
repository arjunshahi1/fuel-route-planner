"""Orchestrates a fuel plan: geocode -> route (1 API call) -> corridor -> optimise."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from fuel.models import RoutePlan

from .corridor import stations_along_route
from .geocoding import LocationError, resolve_location
from .optimizer import FuelPlan, InfeasibleRouteError, StationOption, plan_fuel_stops
from .routing import RoutingError, get_route
from .stations import get_station_index

CENTS = Decimal("0.01")
MAX_RESPONSE_GEOMETRY_POINTS = 750
MAX_STORED_GEOMETRY_POINTS = 5000  # plenty for the map page, keeps saved plans small


class PlanningError(Exception):
    """A user-facing planning failure. ``field`` names the offending input, if any."""

    def __init__(self, message: str, status: int, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field


@dataclass(frozen=True)
class PlanResult:
    plan: RoutePlan
    payload: dict


def build_plan(start_input, finish_input, stop_penalty: float | None = None) -> PlanResult:
    started = time.perf_counter()
    cfg = settings.FUEL_PLANNER
    if stop_penalty is None:
        stop_penalty = cfg["STOP_PENALTY"]

    locations = {}
    for field, value in (("start", start_input), ("finish", finish_input)):
        try:
            locations[field] = resolve_location(value)
        except LocationError as exc:
            raise PlanningError(str(exc), status=400, field=field) from exc
    start, finish = locations["start"], locations["finish"]

    index = get_station_index()  # checked before routing so a misconfigured server spends no API calls
    if len(index) == 0:
        raise PlanningError("No fuel stations are loaded. Run `python manage.py load_fuel_stations`.", status=503)

    try:
        route, cache_hit = get_route((start.latitude, start.longitude), (finish.latitude, finish.longitude))
    except RoutingError as exc:
        raise PlanningError(str(exc), status=exc.status) from exc

    corridor = stations_along_route(route.coordinates, route.distance_miles, index, cfg["CORRIDOR_MILES"])
    options = [
        StationOption(s.mile_marker, s.price, ref=s, detour_miles=2 * s.off_route_miles) for s in corridor
    ]
    common = dict(
        route_miles=route.distance_miles,
        range_miles=cfg["VEHICLE_RANGE_MILES"],
        mpg=cfg["VEHICLE_MPG"],
        start_radius_miles=cfg["START_RADIUS_MILES"],
    )
    try:
        fuel_plan = plan_fuel_stops(options, stop_penalty=stop_penalty, **common)
        cheapest = fuel_plan if stop_penalty == 0 else plan_fuel_stops(options, stop_penalty=0, **common)
    except InfeasibleRouteError as exc:
        raise PlanningError(str(exc), status=422) from exc

    plan = RoutePlan(
        start_query=start.query,
        finish_query=finish.query,
        result={},
        route_coordinates=_decimate(route.coordinates, MAX_STORED_GEOMETRY_POINTS),
    )
    stops = _format_stops(fuel_plan, index)
    total = _total(stops)
    payload = {
        "id": str(plan.id),
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "route": {
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_seconds / 3600, 2),
            "provider": route.provider,
        },
        "vehicle": {
            "range_miles": cfg["VEHICLE_RANGE_MILES"],
            "mpg": cfg["VEHICLE_MPG"],
            "tank_capacity_gallons": round(cfg["VEHICLE_RANGE_MILES"] / cfg["VEHICLE_MPG"], 2),
        },
        "summary": {
            "total_fuel_cost": float(total),
            "total_gallons_purchased": round(fuel_plan.total_gallons, 2),
            "starting_fuel_gallons": round(fuel_plan.starting_fuel_gallons, 2),
            # Burned on the trip: driving distance plus detours to the stations used.
            "fuel_used_gallons": round(
                (route.distance_miles + sum(p.station.detour_miles for p in fuel_plan.purchases)) / cfg["VEHICLE_MPG"],
                2,
            ),
            "number_of_stops": len(stops),
            "average_price_per_gallon": (
                round(float(total) / fuel_plan.total_gallons, 3) if fuel_plan.total_gallons else None
            ),
            "stations_in_corridor": len(corridor),
            "stop_penalty": stop_penalty,
        },
        "fuel_stops": stops,
        "cheapest_possible": _cheapest_section(cheapest, index),
        "map_url": f"/api/v1/route-plans/{plan.id}/map/",
        "assumptions": _assumptions(fuel_plan, stop_penalty, cfg),
    }
    payload["meta"] = {
        "external_api_calls": {
            "routing": 0 if cache_hit else 1,
            "geocoding": sum(loc.external_call for loc in (start, finish)),
        },
        "route_cache_hit": cache_hit,
        "processing_time_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    plan.result = payload  # stored without the GeoJSON, which is rebuilt from route_coordinates
    plan.save()
    return PlanResult(plan=plan, payload=with_map(payload, route.coordinates))


def with_map(payload: dict, coordinates: list[list[float]]) -> dict:
    """Add the GeoJSON ``map`` to a stored or freshly built payload."""
    return {**payload, "map": _geojson(coordinates, payload)}


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _format_stops(fuel_plan: FuelPlan, index) -> list[dict]:
    stops = []
    for number, purchase in enumerate(fuel_plan.purchases, start=1):
        route_station = purchase.station.ref
        record = index.records[route_station.index]
        price: Decimal = record["price"]
        stops.append(
            {
                "stop_number": number,
                "opis_id": record["opis_id"],
                "name": record["name"],
                "address": record["address"],
                "city": record["city"],
                "state": record["state"],
                "lat": record["latitude"],
                "lng": record["longitude"],
                "mile_marker": round(route_station.mile_marker, 1),
                "off_route_miles": round(route_station.off_route_miles, 1),
                "price_per_gallon": float(price),
                "fuel_on_arrival_gallons": round(purchase.fuel_on_arrival_gallons, 3),
                "gallons_purchased": round(purchase.gallons, 3),
                "lead_in_gallons": round(purchase.lead_in_gallons, 3),
                "detour_gallons": round(purchase.detour_gallons, 3),
                # What is paid at this pump, in cents; totals are sums of these so they always add up.
                "cost": float(_money(Decimal(repr(purchase.gallons)) * price)),
            }
        )
    return stops


def _total(stops: list[dict]) -> Decimal:
    return _money(sum((Decimal(str(s["cost"])) for s in stops), Decimal("0")))


def _cheapest_section(cheapest: FuelPlan, index) -> dict:
    """The pure minimum-cost plan (no stop penalty), shown next to the default plan."""
    stops = _format_stops(cheapest, index)
    return {"total_fuel_cost": float(_total(stops)), "number_of_stops": len(stops), "fuel_stops": stops}


def _assumptions(fuel_plan: FuelPlan, stop_penalty: float, cfg: dict) -> list[str]:
    range_miles, mpg = cfg["VEHICLE_RANGE_MILES"], cfg["VEHICLE_MPG"]
    if fuel_plan.starting_fuel_gallons:
        start = (
            f"No fuel station is within {cfg['START_RADIUS_MILES']:g} miles of the start, so the vehicle is "
            f"assumed to start with a full tank ({fuel_plan.starting_fuel_gallons:g} gal). That fuel is not "
            "included in the cost."
        )
    else:
        start = (
            "The trip starts with an empty tank; the first fill-up is at a station within "
            f"{cfg['START_RADIUS_MILES']:g} miles of the start, and the fuel used to reach it (lead_in_gallons) "
            "is charged there."
        )
    return [
        f"Vehicle range {range_miles:g} miles at {mpg:g} MPG ({range_miles / mpg:g}-gallon tank).",
        start,
        "The vehicle arrives at the destination with an empty tank.",
        f"Stops are chosen to minimise fuel cost + ${stop_penalty:g} per stop, so a stop is only made when it "
        "saves more than that; cheapest_possible shows the pure minimum-cost plan for comparison.",
        f"Stations within {cfg['CORRIDOR_MILES']:g} miles of the route are considered. The fuel for the round "
        "trip off the route (detour_gallons) is bought at that station and included in its cost; station "
        "locations are approximated by their city.",
        "Duplicate price rows for a station are resolved to the lowest price.",
    ]


def _decimate(coordinates: list[list[float]], max_points: int) -> list[list[float]]:
    if len(coordinates) <= max_points:
        return coordinates
    step = math.ceil(len(coordinates) / max_points)
    sampled = coordinates[::step]
    if sampled[-1] != coordinates[-1]:
        sampled.append(coordinates[-1])
    return sampled


def _point(lng: float, lat: float, properties: dict) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]}, "properties": properties}


def _geojson(coordinates, payload: dict) -> dict:
    """GeoJSON FeatureCollection (route + start/finish + fuel stops); paste into geojson.io to view."""
    start, finish = payload["start"], payload["finish"]
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": _decimate(coordinates, MAX_RESPONSE_GEOMETRY_POINTS)},
            "properties": {"kind": "route"},
        },
        _point(start["lng"], start["lat"], {"kind": "start", "label": start["label"]}),
        _point(finish["lng"], finish["lat"], {"kind": "finish", "label": finish["label"]}),
    ]
    for stop in payload["fuel_stops"]:
        features.append(
            _point(
                stop["lng"],
                stop["lat"],
                {
                    "kind": "fuel_stop",
                    "stop_number": stop["stop_number"],
                    "name": stop["name"],
                    "price_per_gallon": stop["price_per_gallon"],
                    "gallons_purchased": stop["gallons_purchased"],
                    "cost": stop["cost"],
                },
            )
        )
    return {"type": "FeatureCollection", "features": features}
