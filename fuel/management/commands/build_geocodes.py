"""Geocode every station in the fuel price file, fully offline.

Run once; the outputs are committed, so a normal setup never needs to run it:

* ``data/station_geocodes.csv`` - coordinates for every US station (by OPIS ID)
* ``data/us_places.csv``        - compact gazetteer used to resolve user input
  (Census places + populated GeoNames places)

Sources (public, free, downloaded into ``data/raw/`` which is git-ignored):

1. US Census Gazetteer - incorporated places & CDPs
2. US Census Gazetteer - county subdivisions (New England towns, townships)
3. GeoNames US dump - populated places (unincorporated hamlets such as
   "Ruther Glen, VA" that host many truck stops)

Incorporated places are tried first. County subdivisions and GeoNames are
then pooled together, because a township and an unincorporated village with
the same name can be far apart. When the candidates for a name disagree by
more than ``SAME_PLACE_MILES``, the candidate closest to the station's
*Rack ID* centroid wins. A rack is a regional wholesale fuel terminal, so
stations that share one are clustered together; the centroid is computed from
the stations that geocoded unambiguously.
"""

from __future__ import annotations

import csv
import io
import re
import statistics
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuel.services.geo import haversine_miles
from fuel.services.places import US_STATES, name_key, name_variants

CENSUS_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/{year}_Gaz_{kind}_national.zip"
GEONAMES_URL = "https://download.geonames.org/export/dump/US.zip"

# Candidates within this distance of each other are treated as the same place.
SAME_PLACE_MILES = 20.0
# Report (but keep) stations geocoded unusually far from their rack centroid.
RACK_OUTLIER_MILES = 300.0

# Names no gazetteer resolves; checked by hand against the station's highway exit.
ALIASES = {("PUEBLO OF ACOMA", "NM"): "Acoma Pueblo"}
MANUAL_COORDINATES = {
    ("WILLOW BEACH", "AZ"): (35.87026, -114.65942),  # US-93; GeoNames lists it as a spot, not a town
    ("LEWIS", "NY"): (44.27505, -73.56569),  # I-87 exit 32, Essex County (not Lewis County)
    ("JOHNSON", "KS"): (37.57057, -101.75100),  # Johnson City, Stanton County (US-160)
    ("PLEASANT HILL", "NC"): (36.53210, -77.53553),  # I-95 exit 180, Northampton County
}
# Lower number = preferred when candidates agree (GeoNames points are the settlement itself).
SOURCE_PRIORITY = {"census_place": 0, "geonames": 1, "census_cousub": 2}


@dataclass
class Candidate:
    source: str
    name: str
    latitude: float
    longitude: float
    weight: float  # land area (Census) or population (GeoNames); larger wins ties


@dataclass
class Resolution:
    latitude: float
    longitude: float
    source: str
    method: str


def strip_census_suffix(name: str) -> str:
    """Drop the Census type suffix from a place name.

    'Abbeville city' -> 'Abbeville';
    'Nashville-Davidson metropolitan government (balance)' -> 'Nashville-Davidson'.
    """
    name = re.sub(r"\s*\(balance\)$", "", name.strip())
    while True:
        match = re.search(r"\s+(CDP|CCD|UT|[a-z][a-z/\-]*)$", name)
        if not match:
            return name
        name = name[: match.start()]


class Command(BaseCommand):
    help = "Geocode fuel stations by city/state using offline public gazetteers."

    def add_arguments(self, parser):
        data_dir = Path(settings.BASE_DIR) / "data"
        parser.add_argument("--csv", default=str(data_dir / "fuel-prices.csv"))
        parser.add_argument("--raw-dir", default=str(data_dir / "raw"))
        parser.add_argument("--census-year", default="2025")
        parser.add_argument("--stations-out", default=str(data_dir / "station_geocodes.csv"))
        parser.add_argument("--places-out", default=str(data_dir / "us_places.csv"))

    # -- downloads ---------------------------------------------------------

    def _download(self, url: str, dest: Path) -> Path:
        if dest.exists():
            return dest
        self.stdout.write(f"Downloading {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=120) as resp:
            if resp.status_code != 200:
                raise CommandError(f"Download failed ({resp.status_code}): {url}")
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            tmp.rename(dest)
        return dest

    @staticmethod
    def _open_zip_member(path: Path, suffix: str):
        archive = zipfile.ZipFile(path)
        member = next(n for n in archive.namelist() if n.endswith(suffix))
        return io.TextIOWrapper(archive.open(member), encoding="utf-8")

    # -- gazetteer loading -------------------------------------------------

    def _load_census(self, path: Path, source: str) -> dict[tuple[str, str], list[Candidate]]:
        index: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
        with self._open_zip_member(path, ".txt") as fh:
            reader = csv.reader(fh, delimiter="|")
            header = [h.strip() for h in next(reader)]
            for row in reader:
                rec = dict(zip(header, (c.strip() for c in row)))
                if rec["USPS"] not in US_STATES:
                    continue
                name = strip_census_suffix(rec["NAME"])
                index[(rec["USPS"], name_key(name))].append(
                    Candidate(
                        source, name, float(rec["INTPTLAT"]), float(rec["INTPTLONG"]), float(rec["ALAND_SQMI"] or 0)
                    )
                )
        return index

    def _load_geonames(self, path: Path) -> dict[tuple[str, str], list[Candidate]]:
        index: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
        with self._open_zip_member(path, "US.txt") as fh:
            for line in fh:
                cols = line.rstrip("\n").split("\t")
                # feature class P = populated place; skip abandoned/historical/destroyed ones
                if cols[6] != "P" or cols[7] in {"PPLQ", "PPLH", "PPLW"} or "(historical)" in cols[1]:
                    continue
                state = cols[10]
                if state not in US_STATES:
                    continue
                index[(state, name_key(cols[1]))].append(
                    Candidate("geonames", cols[1], float(cols[4]), float(cols[5]), float(cols[14] or 0))
                )
        return index

    # -- resolution --------------------------------------------------------

    @staticmethod
    def _candidates(city: str, state: str, tiers) -> list[Candidate] | None:
        """Candidates from the first tier that knows the name (tiers may pool several sources)."""
        lookup_name = ALIASES.get((city.upper(), state), city)
        for variant in name_variants(lookup_name):
            key = (state, name_key(variant))
            for indexes in tiers:
                found = [c for index in indexes for c in index.get(key, ())]
                if found:
                    return sorted(found, key=lambda c: (SOURCE_PRIORITY[c.source], -c.weight))
        return None

    @staticmethod
    def _is_unambiguous(candidates: list[Candidate]) -> bool:
        best = candidates[0]
        return all(
            haversine_miles(best.latitude, best.longitude, c.latitude, c.longitude) <= SAME_PLACE_MILES
            for c in candidates[1:]
        )

    def handle(self, *args, **opts):
        raw_dir = Path(opts["raw_dir"])
        year = opts["census_year"]
        place_zip = self._download(
            CENSUS_URL.format(year=year, kind="place"), raw_dir / f"{year}_Gaz_place_national.zip"
        )
        cousub_zip = self._download(
            CENSUS_URL.format(year=year, kind="cousubs"), raw_dir / f"{year}_Gaz_cousubs_national.zip"
        )
        geonames_zip = self._download(GEONAMES_URL, raw_dir / "geonames_US.zip")

        self.stdout.write("Loading gazetteers...")
        places = self._load_census(place_zip, "census_place")
        cousubs = self._load_census(cousub_zip, "census_cousub")
        geonames = self._load_geonames(geonames_zip)
        tiers = [[places], [geonames, cousubs]]

        stations = self._read_us_stations(Path(opts["csv"]))
        self.stdout.write(f"{len(stations)} unique US stations to geocode")

        # Pass 1: resolve candidates; unambiguous ones seed the rack centroids.
        pending: dict[int, list[Candidate]] = {}
        resolved: dict[int, Resolution] = {}
        unresolved: list[dict] = []
        for opis_id, st in stations.items():
            manual = MANUAL_COORDINATES.get((st["city"].upper(), st["state"]))
            if manual:
                resolved[opis_id] = Resolution(*manual, source="manual", method="manual")
                continue
            candidates = self._candidates(st["city"], st["state"], tiers)
            if not candidates:
                unresolved.append(st)
                continue
            if self._is_unambiguous(candidates):
                best = candidates[0]
                resolved[opis_id] = Resolution(best.latitude, best.longitude, best.source, "unique")
            else:
                pending[opis_id] = candidates

        rack_points: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for opis_id, res in resolved.items():
            rack = stations[opis_id]["rack_id"]
            if rack is not None:
                rack_points[rack].append((res.latitude, res.longitude))
        rack_centroid = {
            rack: (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))
            for rack, pts in rack_points.items()
        }

        # Pass 2: disambiguate the rest using the rack centroid.
        for opis_id, candidates in pending.items():
            centroid = rack_centroid.get(stations[opis_id]["rack_id"])
            if centroid:
                best = min(
                    candidates,
                    key=lambda c: haversine_miles(centroid[0], centroid[1], c.latitude, c.longitude),
                )
                method = "rack_disambiguated"
            else:
                best, method = candidates[0], "largest"
            resolved[opis_id] = Resolution(best.latitude, best.longitude, best.source, method)

        self._write_stations(Path(opts["stations_out"]), stations, resolved)
        # County subdivisions are left out: a township can share its name with a
        # village tens of miles away, and unknown names fall back to Nominatim.
        self._write_places(Path(opts["places_out"]), [places, geonames])
        self._report(stations, resolved, unresolved, rack_centroid)

    # -- io ----------------------------------------------------------------

    @staticmethod
    def _read_us_stations(path: Path) -> dict[int, dict]:
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")
        stations: dict[int, dict] = {}
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                state = row["State"].strip().upper()
                if state not in US_STATES:
                    continue
                opis_id = int(row["OPIS Truckstop ID"])
                rack = row["Rack ID"].strip()
                stations.setdefault(
                    opis_id,
                    {
                        "opis_id": opis_id,
                        "city": " ".join(row["City"].split()),
                        "state": state,
                        "rack_id": int(rack) if rack.isdigit() else None,
                    },
                )
        return stations

    def _write_stations(self, path: Path, stations, resolved) -> None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["opis_id", "city", "state", "latitude", "longitude", "source", "method"])
            for opis_id in sorted(resolved):
                st, res = stations[opis_id], resolved[opis_id]
                writer.writerow(
                    [
                        opis_id, st["city"], st["state"],
                        f"{res.latitude:.5f}", f"{res.longitude:.5f}", res.source, res.method,
                    ]
                )
        self.stdout.write(f"Wrote {len(resolved)} station coordinates -> {path}")

    def _write_places(self, path: Path, indexes) -> None:
        """Compact gazetteer for request-time lookups: one entry per name, in priority order."""
        seen: set[tuple[str, str]] = set()
        count = 0
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["name", "state", "latitude", "longitude"])
            for index in indexes:
                for key, candidates in index.items():
                    if key in seen:
                        continue
                    best = max(candidates, key=lambda c: c.weight)
                    # Unpopulated GeoNames hamlets add bulk but little value for user input.
                    if best.source == "geonames" and best.weight <= 0:
                        continue
                    seen.add(key)
                    writer.writerow([best.name, key[0], f"{best.latitude:.5f}", f"{best.longitude:.5f}"])
                    count += 1
        self.stdout.write(f"Wrote {count} gazetteer places -> {path}")

    def _report(self, stations, resolved, unresolved, rack_centroid) -> None:
        by_source = defaultdict(int)
        by_method = defaultdict(int)
        for res in resolved.values():
            by_source[res.source] += 1
            by_method[res.method] += 1
        self.stdout.write(f"By source: {dict(by_source)}")
        self.stdout.write(f"By method: {dict(by_method)}")

        if unresolved:
            self.stdout.write(self.style.WARNING(f"{len(unresolved)} stations could not be geocoded:"))
            for st in unresolved:
                self.stdout.write(f"  {st['opis_id']}: {st['city']}, {st['state']}")

        outliers = []
        for opis_id, res in resolved.items():
            centroid = rack_centroid.get(stations[opis_id]["rack_id"])
            if centroid:
                dist = float(haversine_miles(centroid[0], centroid[1], res.latitude, res.longitude))
                if dist > RACK_OUTLIER_MILES:
                    outliers.append((dist, opis_id))
        if outliers:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(outliers)} stations are >{RACK_OUTLIER_MILES:.0f} mi from their rack centroid "
                    "(kept; review):"
                )
            )
            for dist, opis_id in sorted(outliers, reverse=True):
                st = stations[opis_id]
                self.stdout.write(f"  {opis_id}: {st['city']}, {st['state']} rack {st['rack_id']} ({dist:.0f} mi)")
        self.stdout.write(self.style.SUCCESS("Done."))
