"""Resolve user-supplied start/finish locations to coordinates.

Resolution order (cheapest first):

1. ``{"lat": .., "lng": ..}`` objects or "lat,lng" strings - no API call.
2. "City, ST" / "City, State" - looked up in the local US gazetteer, no API call.
3. Anything else (street addresses, landmarks) - one Nominatim request,
   restricted to the USA.

Coordinates are checked to be inside the contiguous USA using the local
gazetteer (the nearest known US place must be close by).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache
from scipy.spatial import cKDTree

from .geo import chord_to_miles, to_unit_vectors
from .places import Place, load_gazetteer, lookup_place

logger = logging.getLogger(__name__)

# A point is "in the USA" if a known US place is within this distance.
MAX_DISTANCE_TO_US_PLACE_MILES = 30.0
# The fuel data only covers the contiguous states, so trips must stay within them.
NON_CONTIGUOUS_STATES = {"AK", "HI"}
FOREIGN_REGIONS = {
    "CANADA", "MEXICO", "MX", "ALBERTA", "BRITISH COLUMBIA", "MANITOBA", "NEW BRUNSWICK",
    "NEWFOUNDLAND AND LABRADOR", "NOVA SCOTIA", "ONTARIO", "PRINCE EDWARD ISLAND", "QUEBEC",
    "SASKATCHEWAN", "YUKON", "NORTHWEST TERRITORIES", "NUNAVUT",
    # Canadian province/territory codes
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}
GEOCODE_CACHE_SECONDS = 60 * 60 * 24
_COORDINATES = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$")
_ZIP_CODE = re.compile(r"^\d{5}(?:-\d{4})?$")
_COUNTRY_SUFFIX = re.compile(r",\s*(U\.?S\.?A?\.?|United States(?: of America)?)\s*$", re.IGNORECASE)


class LocationError(Exception):
    """The location could not be resolved or is outside the supported area."""


@dataclass(frozen=True)
class Location:
    query: str
    label: str
    latitude: float
    longitude: float
    source: str  # "coordinates" | "gazetteer" | "nominatim" | "nominatim_cache"

    @property
    def external_call(self) -> bool:
        return self.source == "nominatim"

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "label": self.label,
            "lat": round(self.latitude, 6),
            "lng": round(self.longitude, 6),
            "resolved_by": self.source,
        }


def resolve_location(value) -> Location:
    if isinstance(value, dict):
        try:
            lat, lng = float(value["lat"]), float(value["lng"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocationError("Coordinate objects must look like {\"lat\": 41.88, \"lng\": -87.63}.") from exc
        return _from_coordinates(lat, lng, query=f"{lat},{lng}")

    query = " ".join(str(value or "").split())  # echoed back to the caller as typed
    if not query:
        raise LocationError("Location must not be empty.")
    text = _COUNTRY_SUFFIX.sub("", query)  # "Chicago, IL, USA" -> "Chicago, IL"

    match = _COORDINATES.match(text)
    if match:
        return _from_coordinates(float(match.group(1)), float(match.group(2)), query=query)

    if "," in text:
        city, _, region = text.rpartition(",")
        state = _us_state_part(query, region.strip())
        place = lookup_place(city.strip(), state)
        if place:
            if place.state in NON_CONTIGUOUS_STATES:
                raise LocationError(f"'{query}' is outside the contiguous United States.")
            return Location(query, place.label, place.latitude, place.longitude, "gazetteer")

    return _from_nominatim(query, text)


def _us_state_part(query: str, region: str) -> str:
    """Return the state part of "City, ST [ZIP]" without the ZIP.

    Fails fast on "Toronto, ON" or "Monterrey, Mexico" instead of letting the
    geocoder fuzzy-match them to some US street.
    """
    words = [w for w in re.split(r"[\s.]+", region.upper()) if w and not _ZIP_CODE.match(w)]
    # "D.C." / "N. Y." -> "DC" / "NY"
    token = "".join(words) if words and all(len(w) == 1 for w in words) else " ".join(words)
    if token in FOREIGN_REGIONS:
        raise LocationError(f"'{query}' is not in the United States.")
    return token


def _from_coordinates(lat: float, lng: float, query: str) -> Location:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise LocationError(f"'{query}' is not a valid latitude,longitude pair.")
    place, distance = nearest_us_place(lat, lng)
    if distance > MAX_DISTANCE_TO_US_PLACE_MILES:
        raise LocationError(f"'{query}' is not within the contiguous United States.")
    return Location(query, f"{lat:.5f}, {lng:.5f} (near {place.label})", lat, lng, "coordinates")


def _from_nominatim(query: str, text: str) -> Location:
    cache_key = "geocode:v1:" + hashlib.sha256(text.lower().encode()).hexdigest()  # safe for any cache backend
    cached = cache.get(cache_key)
    if cached is not None:
        return Location(query, cached["label"], cached["lat"], cached["lng"], "nominatim_cache")

    cfg = settings.FUEL_PLANNER
    try:
        resp = requests.get(
            f"{cfg['NOMINATIM_BASE_URL'].rstrip('/')}/search",
            params={"q": text, "format": "jsonv2", "countrycodes": "us", "limit": 1},
            headers={"User-Agent": cfg["USER_AGENT"]},
            timeout=cfg["HTTP_TIMEOUT_SECONDS"],
        )
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Nominatim lookup failed for %r: %s", query, exc)
        raise LocationError(
            f"Could not geocode '{query}'. Use 'City, ST' (e.g. 'Dallas, TX') or 'lat,lng'."
        ) from exc
    if not isinstance(results, list) or not results:
        raise LocationError(f"No US location found for '{query}'. Try 'City, ST' (e.g. 'Dallas, TX').")
    try:
        lat, lng = float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Nominatim response for %r: %r", query, results[0])
        raise LocationError(f"Could not geocode '{query}'. Use 'City, ST' (e.g. 'Dallas, TX') or 'lat,lng'.") from exc
    _, distance = nearest_us_place(lat, lng)
    if distance > MAX_DISTANCE_TO_US_PLACE_MILES:
        raise LocationError(f"'{query}' is not within the contiguous United States.")
    label = results[0].get("display_name", query)
    cache.set(cache_key, {"label": label, "lat": lat, "lng": lng}, GEOCODE_CACHE_SECONDS)
    return Location(query, label, lat, lng, "nominatim")


# -- nearest-place lookup ---------------------------------------------------

_tree_lock = threading.Lock()
_tree: tuple[cKDTree, list[Place]] | None = None


def _place_tree() -> tuple[cKDTree, list[Place]]:
    global _tree
    if _tree is None:
        with _tree_lock:
            if _tree is None:
                places = [p for p in load_gazetteer().values() if p.state not in NON_CONTIGUOUS_STATES]
                vectors = to_unit_vectors([p.latitude for p in places], [p.longitude for p in places])
                _tree = (cKDTree(vectors), places)
    return _tree


def nearest_us_place(lat: float, lng: float) -> tuple[Place, float]:
    tree, places = _place_tree()
    chord, idx = tree.query(to_unit_vectors([lat], [lng])[0], k=1)
    return places[int(idx)], float(chord_to_miles(chord))

