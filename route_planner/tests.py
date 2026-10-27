import os
from decimal import Decimal
from tempfile import NamedTemporaryFile
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework import status

from .models import FuelStation
from .services import FuelCoverageError, RouteOptimizerService, UpstreamServiceError


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-optimizer-tests",
    }
}


@override_settings(CACHES=TEST_CACHES)
class RouteServiceTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch.object(RouteOptimizerService, "_request_json")
    def test_geocode_uses_us_limited_nominatim_search(self, request_json):
        request_json.return_value = [
            {
                "lat": "41.8781",
                "lon": "-87.6298",
                "address": {"country_code": "us"},
            }
        ]

        coordinates = RouteOptimizerService.geocode_location("Chicago, IL")

        self.assertEqual(coordinates, (41.8781, -87.6298))
        self.assertTrue(request_json.call_args.args[0].endswith("/search"))
        self.assertEqual(request_json.call_args.kwargs["params"]["q"], "Chicago, IL")
        self.assertEqual(request_json.call_args.kwargs["params"]["countrycodes"], "us")
        self.assertEqual(
            request_json.call_args.kwargs["headers"]["User-Agent"],
            "fuel-optimizer-api/1.0",
        )

    @patch.object(RouteOptimizerService, "_request_json")
    def test_geocode_rejects_non_us_result(self, request_json):
        request_json.return_value = [
            {
                "lat": "43.6532",
                "lon": "-79.3832",
                "address": {"country_code": "ca"},
            }
        ]

        self.assertEqual(RouteOptimizerService.geocode_location("Toronto"), (None, None))

    @patch.object(RouteOptimizerService, "_request_json")
    def test_geocode_caches_successful_results(self, request_json):
        request_json.return_value = [
            {
                "lat": "41.8781",
                "lon": "-87.6298",
                "address": {"country_code": "us"},
            }
        ]

        self.assertEqual(RouteOptimizerService.geocode_location("Chicago, IL"), (41.8781, -87.6298))
        self.assertEqual(RouteOptimizerService.geocode_location("  chicago,   il  "), (41.8781, -87.6298))

        self.assertEqual(request_json.call_count, 1)

    def test_mapping_provider_failure_is_logged(self):
        with patch.object(
            RouteOptimizerService.session,
            "get",
            side_effect=requests.ConnectionError("name resolution failed"),
        ):
            with self.assertLogs("route_planner.services", level="ERROR") as captured:
                with self.assertRaises(UpstreamServiceError):
                    RouteOptimizerService._request_json(
                        "https://nominatim.openstreetmap.org/search",
                        params={"q": "Chicago, IL"},
                    )

        self.assertIn(
            "mapping_provider_request_failed provider=nominatim.openstreetmap.org path=/search",
            captured.output[0],
        )

    @patch.object(RouteOptimizerService, "_request_json")
    def test_osrm_request_uses_documented_route_path(self, request_json):
        request_json.return_value = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 1609.344,
                    "duration": 3600,
                    "geometry": {
                        "coordinates": [[-87.6298, 41.8781], [-88.0, 42.0]],
                    },
                    "legs": [
                        {
                            "distance": 1609.344,
                            "annotation": {"distance": [1609.344]},
                        }
                    ],
                }
            ],
        }

        route = RouteOptimizerService.get_osrm_route(
            41.8781,
            -87.6298,
            42.0,
            -88.0,
            annotations=True,
        )

        self.assertIsNotNone(route)
        self.assertTrue(
            request_json.call_args.args[0].endswith(
                "/route/v1/driving/-87.629800,41.878100;-88.000000,42.000000"
            )
        )
        self.assertEqual(request_json.call_args.kwargs["params"]["geometries"], "geojson")
        self.assertEqual(request_json.call_args.kwargs["params"]["annotations"], "distance")

    @patch.object(RouteOptimizerService, "_request_json")
    def test_osrm_rejects_misaligned_route_annotations(self, request_json):
        request_json.return_value = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 3218.688,
                    "duration": 7200,
                    "geometry": {
                        "coordinates": [[-87.6298, 41.8781], [-88.0, 42.0], [-88.2, 42.2]],
                    },
                    "legs": [
                        {
                            "distance": 3218.688,
                            "annotation": {"distance": [3218.688]},
                        }
                    ],
                }
            ],
        }

        with self.assertRaises(UpstreamServiceError):
            RouteOptimizerService.get_osrm_route(
                41.8781,
                -87.6298,
                42.2,
                -88.2,
                annotations=True,
            )


@override_settings(
    CACHES=TEST_CACHES,
    VEHICLE_RANGE_MILES=500,
    FUEL_RESERVE_MILES=50,
    VEHICLE_MILES_PER_GALLON=10,
    FUEL_STATION_CORRIDOR_MILES=4,
)
class FuelOptimizationTests(TestCase):
    def _station(self, source_key, name, latitude, longitude, price):
        return FuelStation.objects.create(
            source_key=source_key,
            name=name,
            address="Interstate Exit",
            city="Test City",
            state="TS",
            latitude=latitude,
            longitude=longitude,
            price_per_gallon=Decimal(price),
        )

    def test_candidate_projection_uses_exact_route_segment_distance(self):
        self._station("station-1", "Precise Stop", 0.02, 0.37, "3.1000")
        route = {
            "distance_miles": 69.0,
            "coordinates": [[0.0, 0.0], [0.0, 1.0]],
            "segment_distances_miles": [69.0],
        }

        with self.assertNumQueries(1):
            candidates = RouteOptimizerService._station_candidates(route)

        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0]["route_miles"], 25.53, places=2)
        self.assertAlmostEqual(candidates[0]["access_miles"], 1.38, places=2)

    def test_optimizer_uses_final_route_legs_for_cost_and_range(self):
        baseline = {
            "distance_miles": 800.0,
            "leg_distances_miles": [800.0],
            "coordinates": [[0.0, 0.0], [0.0, 11.6]],
            "segment_distances_miles": [800.0],
        }
        final_route = {
            "distance_miles": 810.0,
            "leg_distances_miles": [405.0, 405.0],
            "coordinates": [[0.0, 0.0], [0.0, 5.8], [0.0, 11.6]],
            "geojson": {"type": "LineString", "coordinates": [[0.0, 0.0], [5.8, 0.0], [11.6, 0.0]]},
        }
        candidates = [
            {
                "id": 1,
                "name": "Origin Price",
                "address": "Start Road",
                "city": "Test City",
                "state": "TS",
                "latitude": 0.0,
                "longitude": 0.0,
                "price": 3.5,
                "route_miles": 0.0,
                "access_miles": 0.0,
            },
            {
                "id": 2,
                "name": "Lower Price Stop",
                "address": "Midway Road",
                "city": "Test City",
                "state": "TS",
                "latitude": 0.0,
                "longitude": 5.8,
                "price": 2.5,
                "route_miles": 400.0,
                "access_miles": 0.0,
            },
        ]

        with (
            patch.object(RouteOptimizerService, "get_osrm_route", side_effect=[baseline, final_route]) as get_route,
            patch.object(RouteOptimizerService, "_station_candidates", return_value=candidates),
        ):
            route, fuel_plan = RouteOptimizerService.optimize_route(0.0, 0.0, 0.0, 11.6)

        self.assertEqual(route, final_route)
        self.assertEqual(get_route.call_count, 2)
        self.assertEqual([stop["station_name"] for stop in fuel_plan["stops"]], ["Lower Price Stop"])
        self.assertEqual(fuel_plan["stops"][0]["distance_from_start_miles"], 405.0)
        self.assertEqual(fuel_plan["total_gallons_consumed"], 81.0)
        self.assertEqual(fuel_plan["total_fuel_cost"], 243.0)

    def test_optimizer_rejects_an_uncovered_range(self):
        route = {"distance_miles": 800.0}
        origin = {
            "id": 1,
            "name": "Origin Price",
            "address": "Start Road",
            "city": "Test City",
            "state": "TS",
            "latitude": 0.0,
            "longitude": 0.0,
            "price": 3.5,
            "route_miles": 0.0,
            "access_miles": 0.0,
        }

        with self.assertRaises(FuelCoverageError):
            RouteOptimizerService._select_stops(route, [origin])

    def test_optimizer_uses_first_route_station_when_origin_window_is_empty(self):
        route = {"distance_miles": 300.0}
        first_route_station = {
            "id": 1,
            "name": "First Route Station",
            "address": "Highway Exit",
            "city": "Test City",
            "state": "TS",
            "latitude": 0.0,
            "longitude": 1.0,
            "price": 3.5,
            "route_miles": 80.0,
            "access_miles": 1.0,
        }

        with self.assertLogs("route_planner.services", level="WARNING") as captured:
            origin, stops = RouteOptimizerService._select_stops(route, [first_route_station])

        self.assertEqual(origin, first_route_station)
        self.assertEqual(stops, [])
        self.assertIn("fuel_plan_uses_first_route_station_for_initial_price", captured.output[0])


@override_settings(CACHES=TEST_CACHES)
class FuelRouteAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.route_info = {
            "distance_miles": 10.0,
            "duration_hours": 0.2,
            "coordinates": [[41.8781, -87.6298], [41.9, -87.7]],
            "geojson": {
                "type": "LineString",
                "coordinates": [[-87.6298, 41.8781], [-87.7, 41.9]],
            },
        }
        self.fuel_plan = {
            "stops": [],
            "total_fuel_cost": 3.0,
            "initial_fuel_cost": 3.0,
            "total_gallons_consumed": 1.0,
            "route_detour_miles": 0.0,
            "initial_fuel_price_per_gallon": 3.0,
            "initial_fuel_price_station": {
                "station_name": "Start Price",
                "city": "Chicago",
                "state": "IL",
            },
            "vehicle_range_miles": 500.0,
            "reserve_miles": 50.0,
        }

    def test_missing_payload_parameters(self):
        response = self.client.post('/api/plan-route/', {"start_location": "New York"}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch.object(RouteOptimizerService, "geocode_location", return_value=(None, None))
    def test_unresolvable_location_returns_422(self, geocode_location):
        response = self.client.post(
            "/api/plan-route/",
            {"start_location": "Unknown Place", "finish_location": "Chicago, IL"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn("Could not geocode", response.data["error"])

    @patch.object(
        RouteOptimizerService,
        "geocode_location",
        side_effect=UpstreamServiceError("provider unavailable"),
    )
    def test_geocoding_outage_returns_502(self, geocode_location):
        response = self.client.post(
            "/api/plan-route/",
            {"start_location": "Chicago, IL", "finish_location": "St. Louis, MO"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    @patch.object(RouteOptimizerService, "optimize_route")
    @patch.object(RouteOptimizerService, "geocode_location")
    def test_successful_route_generation_returns_map_url(
        self,
        geocode_location,
        optimize_route,
    ):
        geocode_location.side_effect = [(41.8781, -87.6298), (38.6270, -90.1994)]
        optimize_route.return_value = (self.route_info, self.fuel_plan)

        response = self.client.post(
            "/api/plan-route/",
            {
                "start_location": "<img src=x onerror=alert(1)>",
                "finish_location": "St. Louis, MO",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["fuel_optimization"]["number_of_fuel_stops"], 0)
        self.assertTrue(response.data["map_url"].startswith("/api/plan-route/map/?"))
        self.assertNotIn("map_html_raw", response.data)

    @patch.object(RouteOptimizerService, "optimize_route")
    @patch.object(RouteOptimizerService, "geocode_location")
    def test_map_endpoint_returns_rendered_leaflet_document(
        self,
        geocode_location,
        optimize_route,
    ):
        geocode_location.side_effect = [(41.8781, -87.6298), (38.6270, -90.1994)]
        optimize_route.return_value = (self.route_info, self.fuel_plan)

        response = self.client.get(
            "/api/plan-route/map/",
            {
                "start_location": "<img src=x onerror=alert(1)>",
                "finish_location": "St. Louis, MO",
            },
        )

        html = response.content.decode("utf-8")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertIn("leaflet@1.9.4", html)
        self.assertIn("route-marker__pin--start", html)
        self.assertIn("route-marker__pin--finish", html)
        self.assertIn("L.control.layers", html)
        self.assertNotIn("<img src=x", html)

    @patch.object(RouteOptimizerService, "geocode_location", return_value=(None, None))
    def test_map_endpoint_renders_html_error_state(self, geocode_location):
        response = self.client.get(
            "/api/plan-route/map/",
            {"start_location": "Unknown Place", "finish_location": "Chicago, IL"},
        )

        html = response.content.decode("utf-8")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertIn("Route map unavailable", html)
        self.assertIn("Could not geocode", html)

    def test_health_endpoint_returns_ok(self):
        response = self.client.get("/api/health/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"status": "ok", "database": "ok"})


class FuelDataCommandTests(TestCase):
    def test_imports_opis_price_rows_without_coordinates(self):
        with NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as csv_file:
            csv_file.write(
                "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
                "1,TEST FUEL,1 Highway Way,Testville,TS,2,3.4567\n"
            )
            csv_path = csv_file.name

        try:
            call_command("load_fuel_data", csv_path)
        finally:
            os.unlink(csv_path)

        station = FuelStation.objects.get(name="TEST FUEL")
        self.assertEqual(station.price_per_gallon, Decimal("3.4567"))
        self.assertIsNone(station.latitude)
        self.assertIsNone(station.longitude)

    @patch("route_planner.management.commands.load_fuel_data.time.sleep")
    @patch.object(RouteOptimizerService, "geocode_location")
    def test_limit_imports_only_requested_number_of_geocoded_records(
        self,
        geocode_location,
        sleep,
    ):
        geocode_location.side_effect = [(41.8781, -87.6298), (38.6270, -90.1994)]
        with NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as csv_file:
            csv_file.write(
                "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
                "1,FIRST FUEL,1 Highway Way,First City,TS,2,3.4567\n"
                "2,SECOND FUEL,2 Highway Way,Second City,TS,2,3.5567\n"
                "3,THIRD FUEL,3 Highway Way,Third City,TS,2,3.6567\n"
            )
            csv_path = csv_file.name

        try:
            call_command("load_fuel_data", csv_path, limit=2)
        finally:
            os.unlink(csv_path)

        self.assertEqual(FuelStation.objects.count(), 2)
        self.assertListEqual(
            list(FuelStation.objects.order_by("name").values_list("name", flat=True)),
            ["FIRST FUEL", "SECOND FUEL"],
        )
        self.assertListEqual(
            list(FuelStation.objects.order_by("name").values_list("latitude", "longitude")),
            [(41.8781, -87.6298), (38.627, -90.1994)],
        )
        self.assertEqual(geocode_location.call_count, 2)
        sleep.assert_called_once_with(1.1)
