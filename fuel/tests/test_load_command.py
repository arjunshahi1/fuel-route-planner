import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from fuel.models import FuelStation

PRICES_CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
105,TA SAGINAW,"I-75, EXIT 144-B",Bridgeport,MI,260,3.339
105,TA SAGINAW,"I-75, EXIT 144-B",Bridgeport,MI,260,3.269
105,TA SAGINAW,"I-75, EXIT 144-B",Bridgeport,MI,260,3.429
71108,Stuckeyâ€™s Travel Center West,"I-10, EXIT 819",Anahuac,TX,610,2.80733333
79,DELAWARE TRUCK PLAZA,US-13 & US-40,New Castle                              ,DE,243,3.249
629,FLYING J #850,TCH-16,Edmonton,AB,80,4.39948962
999,NO GEOCODE STOP,US-1,Nowhere,KS,1,3.00
"""

GEOCODES_CSV = """opis_id,city,state,latitude,longitude,source,method
79,New Castle,DE,39.66,-75.57,census_place,unique
105,Bridgeport,MI,43.37,-83.88,census_place,unique
71108,Anahuac,TX,29.77,-94.68,census_place,unique
"""


class LoadFuelStationsCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prices = Path(self.tmp.name, "prices.csv")
        self.geocodes = Path(self.tmp.name, "geocodes.csv")
        self.prices.write_text(PRICES_CSV, encoding="utf-8")
        self.geocodes.write_text(GEOCODES_CSV, encoding="utf-8")

    def load(self):
        out = StringIO()
        call_command("load_fuel_stations", csv=str(self.prices), geocodes=str(self.geocodes), stdout=out)
        return out.getvalue()

    def test_dedupes_keeps_cheapest_and_skips_non_us(self):
        output = self.load()
        self.assertEqual(FuelStation.objects.count(), 3)
        self.assertFalse(FuelStation.objects.filter(opis_id=629).exists())  # Canada
        saginaw = FuelStation.objects.get(opis_id=105)
        self.assertEqual(saginaw.price, Decimal("3.269"))
        self.assertEqual(saginaw.price_observations, 3)
        self.assertIn("Skipped (no coordinates):   1", output)

    def test_cleans_text_and_keeps_full_price_precision(self):
        self.load()
        stuckeys = FuelStation.objects.get(opis_id=71108)
        self.assertEqual(stuckeys.name, "Stuckey’s Travel Center West")
        self.assertEqual(stuckeys.price, Decimal("2.80733333"))
        self.assertEqual(FuelStation.objects.get(opis_id=79).city, "New Castle")

    def test_is_idempotent(self):
        self.load()
        self.load()
        self.assertEqual(FuelStation.objects.count(), 3)
