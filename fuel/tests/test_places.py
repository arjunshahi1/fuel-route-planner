from django.test import SimpleTestCase

from fuel.services.geocoding import LocationError, resolve_location
from fuel.services.places import lookup_place, name_key, name_variants, normalize_state


class NameKeyTests(SimpleTestCase):
    def test_spacing_punctuation_and_saint_are_normalised(self):
        self.assertEqual(name_key("Mc Calla"), name_key("McCalla"))
        self.assertEqual(name_key("Saint Johns"), name_key("St. Johns"))
        self.assertEqual(name_key("Bois D Arc"), name_key("Bois D'Arc"))
        self.assertEqual(name_key("Cañon City"), name_key("Canon City"))
        self.assertEqual(name_key("Fort Wayne"), name_key("Ft. Wayne"))

    def test_different_names_stay_different(self):
        self.assertNotEqual(name_key("Lake City"), name_key("Lake"))

    def test_variants(self):
        self.assertIn("South Coffeyville", list(name_variants("S Coffeyville")))
        self.assertIn("Hot Springs", list(name_variants("Hot Springs National Park")))
        self.assertEqual(next(name_variants("  Big   Cabin ")), "Big Cabin")


class StateTests(SimpleTestCase):
    def test_codes_and_names(self):
        self.assertEqual(normalize_state("tx"), "TX")
        self.assertEqual(normalize_state("New Mexico"), "NM")
        self.assertIsNone(normalize_state("ON"))


class GazetteerLookupTests(SimpleTestCase):
    """Uses the committed data/us_places.csv."""

    def test_finds_major_cities(self):
        chicago = lookup_place("Chicago", "IL")
        self.assertIsNotNone(chicago)
        self.assertAlmostEqual(chicago.latitude, 41.84, delta=0.2)
        self.assertAlmostEqual(chicago.longitude, -87.68, delta=0.2)

    def test_state_name_and_abbreviation_variants(self):
        self.assertEqual(lookup_place("St Louis", "Missouri"), lookup_place("Saint Louis", "MO"))

    def test_same_city_name_in_different_states(self):
        tx, il = lookup_place("El Paso", "TX"), lookup_place("El Paso", "IL")
        self.assertLess(tx.longitude, -104)
        self.assertGreater(il.longitude, -90)

    def test_unknown_place(self):
        self.assertIsNone(lookup_place("Definitely Not A Town", "KS"))
        self.assertIsNone(lookup_place("Toronto", "ON"))


class ResolveLocationInputTests(SimpleTestCase):
    """Input formats that must resolve locally (no network)."""

    def test_formats(self):
        for query in ("Chicago, IL", "Chicago, Illinois", "Chicago, IL 60601", "Chicago, IL 60601-1234",
                      "Chicago, IL, USA", "chicago, il, US", "Chicago, IL, United States"):
            location = resolve_location(query)
            self.assertEqual((location.source, location.label), ("gazetteer", "Chicago, IL"), query)
            self.assertEqual(location.query, query)  # echoed back as typed
        self.assertEqual(resolve_location("Washington, D.C.").label, "Washington, DC")
        self.assertEqual(resolve_location("41.88, -87.63").source, "coordinates")
        self.assertEqual(resolve_location("+41.88,-87.63").source, "coordinates")

    def test_foreign_and_unknown_states_are_rejected(self):
        for query in ("Toronto, ON", "Vancouver, BC", "Toronto, Ontario", "Monterrey, Mexico", "Calgary, AB, Canada"):
            with self.assertRaises(LocationError, msg=query):
                resolve_location(query)
