"""In-memory station index.

The station table is small (~6.6k rows), so it is loaded once per process into
numpy arrays. Requests then never touch the database for station data.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from .geo import to_unit_vectors


@dataclass(frozen=True)
class StationIndex:
    ids: np.ndarray  # FuelStation primary keys
    latitudes: np.ndarray
    longitudes: np.ndarray
    prices: np.ndarray  # USD/gal as float, for optimisation maths
    unit_vectors: np.ndarray  # (n, 3) points on the unit sphere
    records: tuple[dict, ...]  # display data, aligned with the arrays

    def __len__(self) -> int:
        return len(self.ids)


_lock = threading.Lock()
_index: StationIndex | None = None


def get_station_index() -> StationIndex:
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                index = _build_index()
                if not len(index):
                    return index  # not cached, so stations loaded later are picked up
                _index = index
    return _index


def clear_station_index() -> None:
    global _index
    with _lock:
        _index = None


def _build_index() -> StationIndex:
    from fuel.models import FuelStation  # avoid app-registry import at module load

    rows = list(
        FuelStation.objects.order_by("id").values(
            "id", "opis_id", "name", "address", "city", "state", "price", "latitude", "longitude"
        )
    )
    lat = np.array([r["latitude"] for r in rows], dtype=float)
    lon = np.array([r["longitude"] for r in rows], dtype=float)
    records = tuple(
        {
            "opis_id": r["opis_id"],
            "name": r["name"],
            "address": r["address"],
            "city": r["city"],
            "state": r["state"],
            "price": r["price"],  # Decimal, exact value from the source file
            "latitude": r["latitude"],
            "longitude": r["longitude"],
        }
        for r in rows
    )
    return StationIndex(
        ids=np.array([r["id"] for r in rows], dtype=np.int64),
        latitudes=lat,
        longitudes=lon,
        prices=np.array([float(r["price"]) for r in rows], dtype=float),
        unit_vectors=to_unit_vectors(lat, lon) if rows else np.empty((0, 3)),
        records=records,
    )
