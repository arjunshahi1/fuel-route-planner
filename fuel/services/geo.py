"""Small, dependency-light geodesy helpers (numpy only)."""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles. Works on scalars or numpy arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def to_unit_vectors(lat, lon) -> np.ndarray:
    """Convert lat/lon (degrees) to points on the unit sphere, shape (n, 3).

    Euclidean (chord) distance between unit vectors is monotonic in
    great-circle distance, so a KD-tree over them gives exact nearest
    neighbours on the sphere.
    """
    lat = np.radians(np.asarray(lat, dtype=float))
    lon = np.radians(np.asarray(lon, dtype=float))
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def chord_to_miles(chord):
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2.0, 0.0, 1.0))


def densify(lats: np.ndarray, lons: np.ndarray, max_step_miles: float):
    """Insert interpolated vertices so no segment is longer than ``max_step_miles``.

    Returns (lats, lons, cumulative_miles). Linear interpolation in lat/lon is
    accurate enough at sub-mile steps.
    """
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    seg = haversine_miles(lats[:-1], lons[:-1], lats[1:], lons[1:])
    seg_start = np.concatenate(([0.0], np.cumsum(seg)[:-1]))
    pieces = np.maximum(1, np.ceil(seg / max_step_miles).astype(int))

    # Segment i contributes points at t = 1/n, 2/n, ..., 1 (vectorised).
    seg_idx = np.repeat(np.arange(len(seg)), pieces)
    offsets = np.repeat(np.cumsum(pieces) - pieces, pieces)
    t = (np.arange(len(seg_idx)) - offsets + 1) / np.repeat(pieces, pieces)

    out_lat = lats[seg_idx] + (lats[seg_idx + 1] - lats[seg_idx]) * t
    out_lon = lons[seg_idx] + (lons[seg_idx + 1] - lons[seg_idx]) * t
    out_cum = seg_start[seg_idx] + seg[seg_idx] * t
    return (
        np.concatenate((lats[:1], out_lat)),
        np.concatenate((lons[:1], out_lon)),
        np.concatenate(([0.0], out_cum)),
    )
