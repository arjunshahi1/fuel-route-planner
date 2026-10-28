"""Load the OPIS fuel price file into the database.

* Canadian rows (AB, ON, BC, ...) are skipped - the API is USA only.
* Rows are deduplicated by OPIS Truckstop ID. The file repeats some stations
  with different prices; the cheapest price is kept, as that is what a driver
  optimising for cost would pay.
* Coordinates come from ``data/station_geocodes.csv`` (see ``build_geocodes``).

The command is idempotent: it replaces the station table on every run.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuel.models import FuelStation
from fuel.services.places import US_STATES
from fuel.services.stations import clear_station_index


def clean_text(value: str) -> str:
    """Collapse whitespace and repair UTF-8 text that was mis-decoded as cp1252 ('Stuckeyâ€™s')."""
    value = " ".join((value or "").split())
    if "â" in value or "Ã" in value:
        try:
            value = value.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return value


class Command(BaseCommand):
    help = "Load fuel stations (deduplicated, geocoded) from the OPIS price CSV."

    def add_arguments(self, parser):
        data_dir = Path(settings.BASE_DIR) / "data"
        parser.add_argument("--csv", default=str(data_dir / "fuel-prices.csv"))
        parser.add_argument("--geocodes", default=str(data_dir / "station_geocodes.csv"))

    def handle(self, *args, **opts):
        csv_path, geocode_path = Path(opts["csv"]), Path(opts["geocodes"])
        for path in (csv_path, geocode_path):
            if not path.exists():
                raise CommandError(f"File not found: {path}")

        with geocode_path.open(newline="", encoding="utf-8") as fh:
            geocodes = {int(row["opis_id"]): row for row in csv.DictReader(fh)}

        total_rows = skipped_non_us = skipped_bad_price = 0
        stations: dict[int, dict] = {}
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                total_rows += 1
                state = row["State"].strip().upper()
                if state not in US_STATES:
                    skipped_non_us += 1
                    continue
                try:
                    price = Decimal(row["Retail Price"].strip())
                    opis_id = int(row["OPIS Truckstop ID"])
                except (InvalidOperation, ValueError):
                    skipped_bad_price += 1
                    continue
                if price <= 0:
                    skipped_bad_price += 1
                    continue

                existing = stations.get(opis_id)
                if existing:
                    existing["price_observations"] += 1
                    if price >= existing["price"]:
                        continue
                    observations = existing["price_observations"]
                else:
                    observations = 1
                rack = row["Rack ID"].strip()
                stations[opis_id] = {
                    "opis_id": opis_id,
                    "name": clean_text(row["Truckstop Name"]),
                    "address": clean_text(row["Address"]).strip(" ,"),
                    "city": clean_text(row["City"]),
                    "state": state,
                    "rack_id": int(rack) if rack.isdigit() else None,
                    "price": price,
                    "price_observations": observations,
                }

        missing_geocode = [sid for sid in stations if sid not in geocodes]
        objects = []
        for opis_id, data in stations.items():
            geo = geocodes.get(opis_id)
            if geo is None:
                continue
            objects.append(
                FuelStation(
                    **data,
                    latitude=float(geo["latitude"]),
                    longitude=float(geo["longitude"]),
                    geocode_source=geo["source"],
                )
            )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(objects, batch_size=1000)
        clear_station_index()

        self.stdout.write(f"CSV rows read:              {total_rows}")
        self.stdout.write(f"Skipped (non-US):           {skipped_non_us}")
        self.stdout.write(f"Skipped (invalid price/id): {skipped_bad_price}")
        self.stdout.write(f"Unique US stations:         {len(stations)}")
        if missing_geocode:
            self.stdout.write(
                self.style.WARNING(f"Skipped (no coordinates):   {len(missing_geocode)} {missing_geocode[:20]}")
            )
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(objects)} fuel stations."))
