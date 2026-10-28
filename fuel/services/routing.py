"""Routing provider clients. Each plan makes at most ONE routing request.

* ``osrm`` - the public OSRM demo server (no API key). Default.
* ``ors``  - OpenRouteService directions (free API key), used when
  ``ORS_API_KEY`` is set or ``ROUTING_PROVIDER=ors``.

Routes are cached by (provider, start, finish) so repeated requests for the
same trip do not call the provider again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from .geo import METERS_PER_MILE

logger = logging.getLogger(__name__)


class RoutingError(Exception):
    """Raised when no route can be obtained. ``status`` is the HTTP status to return."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Route:
    coordinates: list[list[float]]  # [[lng, lat], ...]
    distance_miles: float
    duration_seconds: float
    provider: str


def _config() -> dict:
    return settings.FUEL_PLANNER


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> tuple[Route, bool]:
    """Return (route, from_cache). ``start``/``finish`` are (lat, lng)."""
    provider = _config()["ROUTING_PROVIDER"].lower()
    key = f"route:v1:{provider}:{start[0]:.5f},{start[1]:.5f};{finish[0]:.5f},{finish[1]:.5f}"
    cached = cache.get(key)
    if cached is not None:
        return Route(**cached), True

    if provider == "osrm":
        route = _osrm_route(start, finish)
    elif provider == "ors":
        route = _ors_route(start, finish)
    else:
        raise RoutingError(f"Unknown ROUTING_PROVIDER '{provider}'", status=500)

    cache.set(key, route.__dict__, _config()["ROUTE_CACHE_SECONDS"])
    return route, False


def _request(method: str, url: str, **kwargs) -> requests.Response:
    cfg = _config()
    headers = {"User-Agent": cfg["USER_AGENT"], **kwargs.pop("headers", {})}
    try:
        return requests.request(method, url, headers=headers, timeout=cfg["HTTP_TIMEOUT_SECONDS"], **kwargs)
    except requests.Timeout as exc:
        raise RoutingError("The routing service timed out.", status=504) from exc
    except requests.RequestException as exc:
        logger.warning("Routing request failed: %s", exc)
        raise RoutingError("The routing service is unavailable.", status=502) from exc


def _osrm_route(start, finish) -> Route:
    base = _config()["OSRM_BASE_URL"].rstrip("/")
    url = f"{base}/route/v1/driving/{start[1]:.6f},{start[0]:.6f};{finish[1]:.6f},{finish[0]:.6f}"
    resp = _request(
        "GET",
        url,
        params={"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false"},
    )
    data = _json_object(resp)
    code = data.get("code")
    if code in {"NoRoute", "NoSegment"}:
        raise RoutingError("No drivable route exists between the given locations.", status=422)
    if resp.status_code != 200 or code != "Ok" or not data.get("routes"):
        raise RoutingError(f"Routing failed: {data.get('message') or code or resp.status_code}")

    try:
        best = data["routes"][0]
        return Route(
            coordinates=best["geometry"]["coordinates"],
            distance_miles=float(best["distance"]) / METERS_PER_MILE,
            duration_seconds=float(best["duration"]),
            provider="osrm",
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RoutingError("Routing service returned an unexpected response.") from exc


def _ors_route(start, finish) -> Route:
    api_key = _config()["ORS_API_KEY"]
    if not api_key:
        raise RoutingError("ROUTING_PROVIDER is 'ors' but ORS_API_KEY is not set.", status=500)
    resp = _request(
        "POST",
        "https://api.openrouteservice.org/v2/directions/driving-car/geojson",
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json={
            "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],
            "instructions": False,
            "radiuses": [-1, -1],  # snap to the nearest road however far it is
        },
    )
    data = _json_object(resp)
    if resp.status_code != 200 or not data.get("features"):
        error = data.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        status = 422 if resp.status_code in (400, 404) else 502
        raise RoutingError(f"Routing failed: {message or resp.status_code}", status=status)

    try:
        feature = data["features"][0]
        summary = feature["properties"].get("summary", {})
        return Route(
            coordinates=feature["geometry"]["coordinates"],
            distance_miles=float(summary.get("distance", 0.0)) / METERS_PER_MILE,
            duration_seconds=float(summary.get("duration", 0.0)),
            provider="ors",
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise RoutingError("Routing service returned an unexpected response.") from exc


def _json_object(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise RoutingError(f"Routing service returned an invalid response (HTTP {resp.status_code}).") from exc
    if not isinstance(data, dict):
        raise RoutingError("Routing service returned an unexpected response.")
    return data
