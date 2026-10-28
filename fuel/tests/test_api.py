from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from fuel.models import FuelStation, RoutePlan
from fuel.services.geo import haversine_miles
from fuel.services.routing import Route, RoutingError
from fuel.services.stations import clear_station_index

# A straight route along latitude 40 from -100 to -88 (~633 miles of "road").
ROUTE_COORDS = [[-100.0 + i * 0.25, 40.0] for i in range(49)]
ROUTE_MILES = float(haversine_miles(40, -100, 40, -88))
FAKE_ROUTE = Route(coordinates=ROUTE_COORDS, distance_miles=ROUTE_MILES, duration_seconds=36000, provider="osrm")


def station(opis_id, lng, price, lat=40.02, name=None):
    return FuelStation(
        opis_id=opis_id,
        name=name or f"STATION {opis_id}",
        address="I-00, EXIT 1",
        city="Testville",
        state="KS",
        price=Decimal(price),
        latitude=lat,
        longitude=lng,
    )


class RoutePlanApiTests(TestCase):
    def setUp(self):
        cache.clear()
        clear_station_index()
        self.addCleanup(clear_station_index)
        FuelStation.objects.bulk_create(
            [
                station(1, -99.95, "3.50000000"),  # near the start
                station(2, -97.00, "2.90000000"),  # cheap, ~160 mi
                station(3, -93.00, "3.20000000"),
                station(4, -91.00, "3.00000000"),
                station(5, -95.00, "1.00000000", lat=41.5),  # very cheap but ~100 mi off route
            ]
        )
        self.client = APIClient()
        # Network must never be touched in these tests.
        patcher = mock.patch("fuel.services.planner.get_route", return_value=(FAKE_ROUTE, False))
        self.get_route = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, start="40.0,-100.0", finish="40.0,-88.0"):
        return self.client.post("/api/v1/route-plans/", {"start": start, "finish": finish}, format="json")

    def test_plan_response_shape_and_totals(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()

        self.assertEqual(data["route"]["distance_miles"], round(ROUTE_MILES, 1))
        self.assertEqual(data["summary"]["number_of_stops"], len(data["fuel_stops"]))
        self.assertAlmostEqual(
            data["summary"]["total_gallons_purchased"], data["summary"]["fuel_used_gallons"], places=2
        )
        self.assertGreaterEqual(data["summary"]["fuel_used_gallons"], round(ROUTE_MILES / 10, 2))
        self.assertEqual(data["summary"]["starting_fuel_gallons"], 0)
        self.assertAlmostEqual(
            data["summary"]["total_fuel_cost"], round(sum(s["cost"] for s in data["fuel_stops"]), 2), places=2
        )
        names = [s["name"] for s in data["fuel_stops"]]
        self.assertEqual(names[0], "STATION 1")  # first fill-up near the start
        self.assertIn("STATION 2", names)  # the cheap on-route station is used
        self.assertNotIn("STATION 5", names)  # off-route station is never used
        for stop in data["fuel_stops"]:
            self.assertAlmostEqual(stop["cost"], stop["gallons_purchased"] * stop["price_per_gallon"], delta=0.01)
            # What goes into the tank (lead-in fuel was burned before the pump) fits a 50-gallon tank.
            in_tank = stop["fuel_on_arrival_gallons"] + stop["gallons_purchased"] - stop["lead_in_gallons"]
            self.assertLessEqual(in_tank, 50 + 0.01)

        self.assertTrue(data["map_url"].startswith("http://testserver/api/v1/route-plans/"))
        kinds = [f["properties"]["kind"] for f in data["map"]["features"]]
        self.assertEqual(kinds[:3], ["route", "start", "finish"])
        self.assertEqual(kinds.count("fuel_stop"), len(data["fuel_stops"]))
        self.assertEqual(data["meta"]["external_api_calls"], {"routing": 1, "geocoding": 0})
        self.get_route.assert_called_once()

    def test_city_names_and_location_header(self):
        resp = self.post(start="Hays, KS", finish="Salina, KS")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["start"]["resolved_by"], "gazetteer")
        self.assertEqual(resp["Location"], f"http://testserver/api/v1/route-plans/{resp.json()['id']}/")

    def test_get_on_the_collection_is_not_allowed(self):
        resp = self.client.get("/api/v1/route-plans/", {"start": "Hays, KS", "finish": "Salina, KS"})
        self.assertEqual(resp.status_code, 405)
        self.assertIn("message", resp.json()["error"])

    def test_city_state_with_zip_resolves_locally(self):
        resp = self.post(start="Hays, KS 67601")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["start"]["resolved_by"], "gazetteer")

    def test_accepts_coordinate_objects(self):
        resp = self.post(start={"lat": 40.0, "lng": -100.0}, finish={"lat": 40.0, "lng": -88.0})
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_plan_is_persisted_and_map_page_renders(self):
        data = self.post().json()
        plan = RoutePlan.objects.get(pk=data["id"])
        self.assertEqual(len(plan.route_coordinates), len(ROUTE_COORDS))

        detail = self.client.get(f"/api/v1/route-plans/{data['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["summary"], data["summary"])

        page = self.client.get(f"/api/v1/route-plans/{data['id']}/map/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "leaflet")
        self.assertContains(page, "STATION 1")
        self.assertEqual(self.get_route.call_count, 1)  # the map never re-routes

    def test_stop_penalty_override_and_cheapest_comparison(self):
        cheapest = self.client.post(
            "/api/v1/route-plans/", {"start": "40.0,-100.0", "finish": "40.0,-88.0", "stop_penalty": 0}, format="json"
        ).json()
        self.assertEqual(cheapest["summary"]["stop_penalty"], 0)
        self.assertEqual(
            cheapest["cheapest_possible"],
            {
                "total_fuel_cost": cheapest["summary"]["total_fuel_cost"],
                "number_of_stops": cheapest["summary"]["number_of_stops"],
                "fuel_stops": cheapest["fuel_stops"],
            },
        )
        default = self.post().json()
        self.assertEqual(default["summary"]["stop_penalty"], settings.FUEL_PLANNER["STOP_PENALTY"])
        self.assertGreaterEqual(default["summary"]["total_fuel_cost"], cheapest["summary"]["total_fuel_cost"])
        self.assertLessEqual(default["summary"]["number_of_stops"], cheapest["summary"]["number_of_stops"])

    def test_start_without_a_nearby_station_uses_a_full_tank(self):
        # Like Houston -> Dallas: the price data has no station near the start.
        FuelStation.objects.filter(opis_id=1).delete()
        clear_station_index()
        resp = self.post()
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()
        self.assertEqual(data["summary"]["starting_fuel_gallons"], 50.0)
        self.assertAlmostEqual(
            data["summary"]["fuel_used_gallons"],
            data["summary"]["starting_fuel_gallons"] + data["summary"]["total_gallons_purchased"],
            places=1,
        )
        self.assertNotIn("STATION 1", [s["name"] for s in data["fuel_stops"]])
        self.assertTrue(any("full tank" in a for a in data["assumptions"]))
        page = self.client.get(f"/api/v1/route-plans/{data['id']}/map/")
        self.assertContains(page, "Starts with a full tank")

    def test_invalid_stop_penalty(self):
        resp = self.client.post(
            "/api/v1/route-plans/", {"start": "Hays, KS", "finish": "Salina, KS", "stop_penalty": -1}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["field"], "stop_penalty")

    def test_wrong_location_types_are_400_not_500(self):
        for bad in (5, [1, 2], {"lat": 1}, None, True):
            resp = self.client.post("/api/v1/route-plans/", {"start": bad, "finish": "Salina, KS"}, format="json")
            self.assertEqual(resp.status_code, 400, bad)
            self.assertEqual(resp.json()["error"]["field"], "start")

    def test_missing_field(self):
        resp = self.client.post("/api/v1/route-plans/", {"start": "Hays, KS"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["field"], "finish")

    def test_location_outside_usa(self):
        resp = self.post(start="51.5074,-0.1278")  # London
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["field"], "start")
        self.get_route.assert_not_called()

    def test_foreign_region_is_rejected_without_geocoding(self):
        with mock.patch("fuel.services.geocoding.requests.get") as get:
            for start in ("Toronto, ON", "Toronto, Ontario", "Monterrey, Mexico"):
                resp = self.post(start=start)
                self.assertEqual(resp.status_code, 400, start)
                self.assertIn("not in the United States", resp.json()["error"]["message"])
        get.assert_not_called()

    def test_malformed_json_uses_the_error_shape(self):
        resp = self.client.post("/api/v1/route-plans/", "{bad", content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("JSON parse error", resp.json()["error"]["message"])

    def test_alaska_is_rejected(self):
        resp = self.post(finish="Anchorage, AK")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("contiguous", resp.json()["error"]["message"])

    def test_unknown_free_text_uses_one_geocoding_call_then_cache(self):
        with mock.patch("fuel.services.geocoding.requests.get") as get:
            get.return_value.json.return_value = [{"lat": "40.0", "lon": "-100.0", "display_name": "Somewhere, KS"}]
            get.return_value.raise_for_status.return_value = None
            first = self.post(start="123 Main Street, Somewhere Kansas")
            second = self.post(start="123 Main Street, Somewhere Kansas")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(first.json()["meta"]["external_api_calls"]["geocoding"], 1)
        self.assertEqual(second.json()["meta"]["external_api_calls"]["geocoding"], 0)
        self.assertEqual(second.json()["start"]["resolved_by"], "nominatim_cache")

    def test_malformed_geocoder_response_is_a_400(self):
        for payload in ({"error": "boom"}, [{"display_name": "no coordinates"}], [{"lat": "x", "lon": "y"}]):
            with mock.patch("fuel.services.geocoding.requests.get") as get:
                get.return_value.json.return_value = payload
                get.return_value.raise_for_status.return_value = None
                resp = self.post(start=f"1 Unknown Road {len(str(payload))}, Somewhere Kansas")
            self.assertEqual(resp.status_code, 400, payload)
            self.assertEqual(resp.json()["error"]["field"], "start")

    def test_stations_loaded_after_startup_are_picked_up(self):
        FuelStation.objects.all().delete()
        clear_station_index()
        resp = self.post()
        self.assertEqual(resp.status_code, 503)
        self.get_route.assert_not_called()  # no routing call is spent when nothing can be planned
        FuelStation.objects.bulk_create([station(1, -99.95, "3.5"), station(4, -95.0, "3.0"), station(6, -91.0, "3.1")])
        self.assertEqual(self.post().status_code, 201)

    def test_rate_limited(self):
        from rest_framework.settings import api_settings
        from rest_framework.throttling import AnonRateThrottle

        rates = {**api_settings.DEFAULT_THROTTLE_RATES, "anon": "2/minute"}
        with mock.patch.object(AnonRateThrottle, "THROTTLE_RATES", rates):
            codes = [self.post().status_code for _ in range(3)]
        self.assertEqual(codes, [201, 201, 429])

    def test_infeasible_route(self):
        FuelStation.objects.filter(opis_id__in=[2, 3, 4]).delete()  # leaves a > 500 mile gap
        clear_station_index()
        resp = self.post()
        self.assertEqual(resp.status_code, 422)
        self.assertIn("stretch without a known fuel station", resp.json()["error"]["message"])

    def test_routing_failure_is_reported(self):
        self.get_route.side_effect = RoutingError("No drivable route exists between the given locations.", status=422)
        resp = self.post()
        self.assertEqual(resp.status_code, 422)
        self.assertIn("No drivable route", resp.json()["error"]["message"])

    def test_unknown_plan(self):
        resp = self.client.get("/api/v1/route-plans/00000000-0000-0000-0000-000000000000/map/")
        self.assertEqual(resp.status_code, 404)


class RoutingClientTests(TestCase):
    """The HTTP clients parse provider responses correctly (no real network)."""

    def setUp(self):
        cache.clear()

    def test_osrm_parsing_and_cache(self):
        from fuel.services import routing

        payload = {
            "code": "Ok",
            "routes": [{"distance": 160934.4, "duration": 5400, "geometry": {"coordinates": [[-100, 40], [-99, 40]]}}],
        }
        with self.settings(FUEL_PLANNER={**self._cfg(), "ROUTING_PROVIDER": "osrm"}):
            with mock.patch.object(routing.requests, "request") as req:
                req.return_value.json.return_value = payload
                req.return_value.status_code = 200
                route, cached = routing.get_route((40, -100), (40, -99))
                route2, cached2 = routing.get_route((40, -100), (40, -99))
        self.assertAlmostEqual(route.distance_miles, 100.0)
        self.assertEqual((cached, cached2), (False, True))
        self.assertEqual(req.call_count, 1)
        self.assertEqual(route2, route)

    def test_osrm_no_route(self):
        from fuel.services import routing

        with self.settings(FUEL_PLANNER={**self._cfg(), "ROUTING_PROVIDER": "osrm"}):
            with mock.patch.object(routing.requests, "request") as req:
                req.return_value.json.return_value = {"code": "NoRoute", "message": "Impossible route"}
                req.return_value.status_code = 400
                with self.assertRaises(routing.RoutingError) as ctx:
                    routing.get_route((40, -100), (21, -157))
        self.assertEqual(ctx.exception.status, 422)

    def test_malformed_provider_payload_is_a_routing_error(self):
        from fuel.services import routing

        with self.settings(FUEL_PLANNER={**self._cfg(), "ROUTING_PROVIDER": "osrm"}):
            for payload in ([1, 2], {"code": "Ok", "routes": [{"distance": 1}]}):
                cache.clear()
                with mock.patch.object(routing.requests, "request") as req:
                    req.return_value.json.return_value = payload
                    req.return_value.status_code = 200
                    with self.assertRaises(routing.RoutingError):
                        routing.get_route((40, -100), (40, -99))

    def test_ors_parsing(self):
        from fuel.services import routing

        payload = {
            "features": [
                {
                    "geometry": {"coordinates": [[-100, 40], [-99, 40]]},
                    "properties": {"summary": {"distance": 160934.4, "duration": 5400}},
                }
            ]
        }
        with self.settings(FUEL_PLANNER={**self._cfg(), "ROUTING_PROVIDER": "ors", "ORS_API_KEY": "k"}):
            with mock.patch.object(routing.requests, "request") as req:
                req.return_value.json.return_value = payload
                req.return_value.status_code = 200
                route, _ = routing.get_route((40, -100), (40, -99))
        self.assertEqual(route.provider, "ors")
        self.assertAlmostEqual(route.distance_miles, 100.0)
        _, kwargs = req.call_args
        self.assertEqual(kwargs["json"]["coordinates"], [[-100, 40], [-99, 40]])
        self.assertEqual(kwargs["headers"]["Authorization"], "k")

    @staticmethod
    def _cfg():
        from django.conf import settings

        return dict(settings.FUEL_PLANNER)
