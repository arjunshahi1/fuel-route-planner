"""Find fuel stations within a corridor around a route.

The route polyline is densified to <= 0.5 mile steps and put in a KD-tree of
unit-sphere vectors. Each candidate station (pre-filtered by the route's
bounding box) is matched to its nearest route point, which gives both its
distance from the route and its position ("mile marker") along the route.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .geo import EARTH_RADIUS_MILES, chord_to_miles, densify, to_unit_vectors
from .stations import StationIndex

DENSIFY_STEP_MILES = 0.5


@dataclass(frozen=True)
class RouteStation:
    index: int  # position in the StationIndex arrays
    mile_marker: float  # distance along the route from the start
    off_route_miles: float  # straight-line distance from the route
    price: float


def stations_along_route(
    coordinates: list[list[float]],
    route_miles: float,
    index: StationIndex,
    corridor_miles: float,
) -> list[RouteStation]:
    """Return stations within ``corridor_miles`` of the route, ordered by mile marker.

    ``coordinates`` is the route geometry as [[lng, lat], ...]. Mile markers are
    scaled so the last route point sits exactly at ``route_miles`` (the
    provider's road distance), keeping markers consistent with the total.
    """
    if len(index) == 0 or len(coordinates) < 2:
        return []

    coords = np.asarray(coordinates, dtype=float)
    lons, lats = coords[:, 0], coords[:, 1]

    # Cheap bounding-box prefilter before any spherical maths.
    lat_margin = corridor_miles / 69.0
    max_abs_lat = min(89.0, float(np.max(np.abs(lats))) + lat_margin)
    lon_margin = corridor_miles / (69.172 * np.cos(np.radians(max_abs_lat)))
    in_box = (
        (index.latitudes >= lats.min() - lat_margin)
        & (index.latitudes <= lats.max() + lat_margin)
        & (index.longitudes >= lons.min() - lon_margin)
        & (index.longitudes <= lons.max() + lon_margin)
    )
    candidates = np.flatnonzero(in_box)
    if candidates.size == 0:
        return []

    d_lats, d_lons, cumulative = densify(lats, lons, DENSIFY_STEP_MILES)
    polyline_miles = cumulative[-1]
    scale = route_miles / polyline_miles if polyline_miles > 0 else 0.0

    tree = cKDTree(to_unit_vectors(d_lats, d_lons))
    # Chord length on the unit sphere that corresponds to the corridor width.
    max_chord = 2.0 * np.sin(min(corridor_miles / EARTH_RADIUS_MILES, np.pi) / 2.0)
    chords, nearest = tree.query(index.unit_vectors[candidates], k=1, distance_upper_bound=max_chord * 1.000001)

    found = np.isfinite(chords)
    result = [
        RouteStation(
            index=int(station_idx),
            mile_marker=float(cumulative[point_idx] * scale),
            off_route_miles=float(chord_to_miles(chord)),
            price=float(index.prices[station_idx]),
        )
        for station_idx, point_idx, chord in zip(candidates[found], nearest[found], chords[found])
    ]
    result.sort(key=lambda s: (s.mile_marker, s.price))
    return result
