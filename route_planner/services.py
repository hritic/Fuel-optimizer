import hashlib
import logging
import math
from collections.abc import Iterable
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.cache import cache

from .models import FuelStation


logger = logging.getLogger(__name__)


class UpstreamServiceError(Exception):
    """A geocoding or routing provider could not return a usable response."""


class FuelCoverageError(Exception):
    """Available fuel-price data cannot safely cover the requested trip."""


class RouteOptimizerService:
    MILES_PER_METER = 0.000621371
    MILES_PER_DEGREE_LATITUDE = 69.0
    session = requests.Session()

    @staticmethod
    def _cache_key(prefix: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"fuel_optimizer:{prefix}:{digest}"

    @staticmethod
    def _miles_per_longitude_degree(latitude: float) -> float:
        return RouteOptimizerService.MILES_PER_DEGREE_LATITUDE * max(
            abs(math.cos(math.radians(latitude))),
            0.2,
        )

    @classmethod
    def _request_json(
        cls,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str] | None = None,
        allow_error_payload: bool = False,
    ) -> Any:
        target = urlsplit(url)
        started_at = perf_counter()
        try:
            response = cls.session.get(
                url,
                params=params,
                headers=headers,
                timeout=float(getattr(settings, "ROUTE_HTTP_TIMEOUT_SECONDS", 8)),
                verify=False
            )
            elapsed_ms = round((perf_counter() - started_at) * 1000)
            if response.status_code >= 400:
                logger.warning(
                    "mapping_provider_http_error provider=%s path=%s status=%s reason=%s retry_after=%s elapsed_ms=%s",
                    target.netloc,
                    target.path,
                    response.status_code,
                    response.reason,
                    response.headers.get("Retry-After"),
                    elapsed_ms,
                )
                if not allow_error_payload:
                    raise UpstreamServiceError("The mapping provider returned an HTTP error.")
            payload = response.json()
            logger.debug(
                "mapping_provider_response provider=%s path=%s status=%s elapsed_ms=%s",
                target.netloc,
                target.path,
                response.status_code,
                elapsed_ms,
            )
            return payload
        except requests.RequestException as exc:
            logger.exception(
                "mapping_provider_request_failed provider=%s path=%s elapsed_ms=%s error=%s",
                target.netloc,
                target.path,
                round((perf_counter() - started_at) * 1000),
                exc,
            )
            raise UpstreamServiceError("The mapping provider is unavailable or returned invalid data.") from exc
        except ValueError as exc:
            logger.exception(
                "mapping_provider_invalid_json provider=%s path=%s elapsed_ms=%s",
                target.netloc,
                target.path,
                round((perf_counter() - started_at) * 1000),
            )
            raise UpstreamServiceError("The mapping provider is unavailable or returned invalid data.") from exc

    @classmethod
    def geocode_location(cls, location: str) -> tuple[float | None, float | None]:
        """Resolve a U.S. location and cache successful coordinates."""
        if not location or not location.strip():
            return None, None

        normalized = " ".join(location.casefold().split())
        cache_key = cls._cache_key("geocode", normalized)
        cached = cache.get(cache_key)
        if isinstance(cached, (list, tuple)) and len(cached) == 2:
            return float(cached[0]), float(cached[1])

        params = {
            "q": location.strip(),
            "format": "jsonv2",
            "limit": "1",
            "addressdetails": "1",
            "countrycodes": "us",
        }
        base_url = getattr(settings, "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org").rstrip("/")
        results = cls._request_json(
            f"{base_url}/search",
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": getattr(settings, "NOMINATIM_USER_AGENT", "fuel-optimizer-api/1.0"),
            },
        )
        if not isinstance(results, list) or not results:
            return None, None

        result = results[0]
        address = result.get("address", {}) if isinstance(result, dict) else {}
        if not isinstance(address, dict) or address.get("country_code", "").lower() != "us":
            return None, None

        try:
            latitude = float(result["lat"])
            longitude = float(result["lon"])
        except (KeyError, TypeError, ValueError):
            return None, None
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            return None, None

        cache.set(cache_key, (latitude, longitude), getattr(settings, "GEOCODE_CACHE_TTL_SECONDS", 604800))
        return latitude, longitude

    @classmethod
    def get_osrm_route(
        cls,
        start_lat: float,
        start_lon: float,
        finish_lat: float,
        finish_lon: float,
        *,
        via: Iterable[tuple[float, float]] = (),
        annotations: bool = False,
    ) -> dict[str, Any] | None:
        """Return one OSRM driving route, optionally including fuel-stop waypoints."""
        points = ((start_lat, start_lon), *tuple(via), (finish_lat, finish_lon))
        coordinate_path = ";".join(f"{longitude:.6f},{latitude:.6f}" for latitude, longitude in points)
        cache_key = cls._cache_key("route", f"{int(annotations)}:{coordinate_path}")
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            return cached

        base_url = getattr(settings, "OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
        params = {"overview": "full", "geometries": "geojson", "steps": "false"}
        if annotations:
            params["annotations"] = "distance"
        payload = cls._request_json(
            f"{base_url}/route/v1/driving/{coordinate_path}",
            params=params,
            allow_error_payload=True,
        )
        if not isinstance(payload, dict):
            raise UpstreamServiceError("The routing provider returned an unexpected response.")
        if payload.get("code") in {"NoRoute", "NoSegment"}:
            return None
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise UpstreamServiceError("The routing provider could not calculate the route.")

        try:
            route = payload["routes"][0]
            raw_coordinates = route["geometry"]["coordinates"]
            coordinates = [[float(latitude), float(longitude)] for longitude, latitude in raw_coordinates]
            leg_distances = [float(leg["distance"]) * cls.MILES_PER_METER for leg in route["legs"]]
            distance_miles = float(route["distance"]) * cls.MILES_PER_METER
            duration_hours = float(route["duration"]) / 3600
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise UpstreamServiceError("The routing provider returned malformed route data.") from exc

        if (
            len(coordinates) < 2
            or len(leg_distances) != len(points) - 1
            or not math.isfinite(distance_miles)
            or distance_miles <= 0
        ):
            raise UpstreamServiceError("The routing provider returned an invalid route.")

        result = {
            "distance_miles": round(distance_miles, 2),
            "duration_hours": round(duration_hours, 1),
            "coordinates": coordinates,
            "geojson": {"type": "LineString", "coordinates": raw_coordinates},
            "leg_distances_miles": leg_distances,
        }
        if annotations:
            distances = route["legs"][0].get("annotation", {}).get("distance", [])
            segment_distances = [float(distance) * cls.MILES_PER_METER for distance in distances]
            if len(segment_distances) != len(coordinates) - 1:
                raise UpstreamServiceError("The routing provider returned misaligned route annotations.")
            result["segment_distances_miles"] = segment_distances

        cache.set(cache_key, result, getattr(settings, "ROUTE_CACHE_TTL_SECONDS", 86400))
        return result

    @classmethod
    def _segment_grid(
        cls,
        coordinates: list[list[float]],
        corridor_miles: float,
    ) -> tuple[dict[tuple[int, int], list[int]], float]:
        cell_size = max(corridor_miles / cls.MILES_PER_DEGREE_LATITUDE, 0.02)
        latitude_padding = corridor_miles / cls.MILES_PER_DEGREE_LATITUDE
        grid: dict[tuple[int, int], list[int]] = {}

        for index, (start, end) in enumerate(zip(coordinates, coordinates[1:])):
            longitude_padding = corridor_miles / cls._miles_per_longitude_degree(
                (start[0] + end[0]) / 2
            )
            for latitude_cell in range(
                math.floor((min(start[0], end[0]) - latitude_padding) / cell_size),
                math.floor((max(start[0], end[0]) + latitude_padding) / cell_size) + 1,
            ):
                for longitude_cell in range(
                    math.floor((min(start[1], end[1]) - longitude_padding) / cell_size),
                    math.floor((max(start[1], end[1]) + longitude_padding) / cell_size) + 1,
                ):
                    grid.setdefault((latitude_cell, longitude_cell), []).append(index)
        return grid, cell_size

    @classmethod
    def _project_station(
        cls,
        latitude: float,
        longitude: float,
        start: list[float],
        end: list[float],
    ) -> tuple[float, float]:
        longitude_miles = cls._miles_per_longitude_degree(latitude)
        start_x = (start[1] - longitude) * longitude_miles
        start_y = (start[0] - latitude) * cls.MILES_PER_DEGREE_LATITUDE
        end_x = (end[1] - longitude) * longitude_miles
        end_y = (end[0] - latitude) * cls.MILES_PER_DEGREE_LATITUDE
        segment_x = end_x - start_x
        segment_y = end_y - start_y
        length_squared = segment_x**2 + segment_y**2
        if length_squared == 0:
            return math.hypot(start_x, start_y), 0.0

        fraction = min(
            1.0,
            max(0.0, -(start_x * segment_x + start_y * segment_y) / length_squared),
        )
        nearest_x = start_x + fraction * segment_x
        nearest_y = start_y + fraction * segment_y
        return math.hypot(nearest_x, nearest_y), fraction

    @classmethod
    def _station_candidates(cls, route: dict[str, Any]) -> list[dict[str, Any]]:
        coordinates = route["coordinates"]
        segment_distances = route["segment_distances_miles"]
        corridor_miles = float(getattr(settings, "FUEL_STATION_CORRIDOR_MILES", 4))
        grid, cell_size = cls._segment_grid(coordinates, corridor_miles)
        cumulative_miles = [0.0]
        for distance in segment_distances:
            cumulative_miles.append(cumulative_miles[-1] + distance)

        latitudes = [coordinate[0] for coordinate in coordinates]
        longitudes = [coordinate[1] for coordinate in coordinates]
        longitude_padding = corridor_miles / min(
            cls._miles_per_longitude_degree(latitude) for latitude in latitudes
        )
        stations = FuelStation.objects.filter(
            latitude__isnull=False,
            longitude__isnull=False,
            latitude__range=(
                min(latitudes) - corridor_miles / cls.MILES_PER_DEGREE_LATITUDE,
                max(latitudes) + corridor_miles / cls.MILES_PER_DEGREE_LATITUDE,
            ),
            longitude__range=(min(longitudes) - longitude_padding, max(longitudes) + longitude_padding),
            price_per_gallon__gt=0,
        ).values(
            "id",
            "name",
            "address",
            "city",
            "state",
            "latitude",
            "longitude",
            "price_per_gallon",
        )

        candidates = []
        for station in stations:
            latitude = float(station["latitude"])
            longitude = float(station["longitude"])
            segment_ids = grid.get(
                (math.floor(latitude / cell_size), math.floor(longitude / cell_size)),
                (),
            )
            closest = None
            for segment_index in segment_ids:
                access_miles, fraction = cls._project_station(
                    latitude,
                    longitude,
                    coordinates[segment_index],
                    coordinates[segment_index + 1],
                )
                if access_miles > corridor_miles:
                    continue
                route_miles = cumulative_miles[segment_index] + fraction * segment_distances[segment_index]
                if closest is None or access_miles < closest[0]:
                    closest = (access_miles, route_miles)
            if closest is None:
                continue

            access_miles, route_miles = closest
            candidates.append(
                {
                    "id": station["id"],
                    "name": station["name"],
                    "address": station["address"],
                    "city": station["city"],
                    "state": station["state"],
                    "latitude": latitude,
                    "longitude": longitude,
                    "price": float(station["price_per_gallon"]),
                    "route_miles": route_miles,
                    "access_miles": access_miles,
                }
            )
        return sorted(candidates, key=lambda station: station["route_miles"])

    @staticmethod
    def _can_reach_finish(
        candidates: list[dict[str, Any]],
        total_miles: float,
        usable_range: float,
    ) -> list[bool]:
        viable = [False] * len(candidates)
        closest_viable_target = total_miles
        for index in range(len(candidates) - 1, -1, -1):
            candidate = candidates[index]
            if closest_viable_target <= candidate["route_miles"] - candidate["access_miles"] + usable_range:
                viable[index] = True
                closest_viable_target = min(
                    closest_viable_target,
                    candidate["route_miles"] + candidate["access_miles"],
                )
        return viable

    @classmethod
    def _select_stops(
        cls,
        route: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        vehicle_range = float(getattr(settings, "VEHICLE_RANGE_MILES", 500))
        reserve_miles = min(
            max(float(getattr(settings, "FUEL_RESERVE_MILES", 50)), 0),
            vehicle_range - 1,
        )
        usable_range = vehicle_range - reserve_miles
        origin_limit = float(getattr(settings, "ORIGIN_PRICE_SEARCH_MILES", 25))
        origin_choices = [
            candidate
            for candidate in candidates
            if candidate["route_miles"] + candidate["access_miles"] <= origin_limit
        ]
        if origin_choices:
            origin = min(
                origin_choices,
                key=lambda station: (station["route_miles"], station["access_miles"]),
            )
        else:
            origin = min(
                candidates,
                key=lambda station: (station["route_miles"], station["access_miles"]),
            )
            logger.warning(
                "fuel_plan_uses_first_route_station_for_initial_price station_id=%s route_miles=%.1f",
                origin["id"],
                origin["route_miles"],
            )

        total_miles = float(route["distance_miles"])
        if total_miles <= usable_range:
            return origin, []

        viable = cls._can_reach_finish(candidates, total_miles, usable_range)
        current = {"route_miles": 0.0, "access_miles": 0.0, "price": origin["price"]}
        selected = []
        search_start = 0
        while total_miles - current["route_miles"] + current["access_miles"] > usable_range:
            reachable = []
            for index in range(search_start, len(candidates)):
                candidate = candidates[index]
                if candidate["route_miles"] - current["route_miles"] > usable_range:
                    break
                distance_from_current = (
                    candidate["route_miles"]
                    - current["route_miles"]
                    + candidate["access_miles"]
                    + current["access_miles"]
                )
                if viable[index] and distance_from_current <= usable_range:
                    reachable.append((index, candidate))
            if not reachable:
                raise FuelCoverageError("Fuel-price data cannot safely cover the next range window.")

            cheaper = next(
                (item for item in reachable if item[1]["price"] < current["price"]),
                None,
            )
            selected_index, selected_station = cheaper or min(
                reachable,
                key=lambda item: (item[1]["price"], -item[1]["route_miles"]),
            )
            selected.append(selected_station)
            current = selected_station
            search_start = selected_index + 1

        return origin, selected

    @classmethod
    def _fuel_plan(
        cls,
        origin: dict[str, Any],
        stops: list[dict[str, Any]],
        baseline_distance: float,
        routed_trip: dict[str, Any],
    ) -> dict[str, Any]:
        vehicle_range = float(getattr(settings, "VEHICLE_RANGE_MILES", 500))
        reserve_miles = min(
            max(float(getattr(settings, "FUEL_RESERVE_MILES", 50)), 0),
            vehicle_range - 1,
        )
        usable_range = vehicle_range - reserve_miles
        miles_per_gallon = float(getattr(settings, "VEHICLE_MILES_PER_GALLON", 10))
        legs = routed_trip["leg_distances_miles"]
        if len(legs) != len(stops) + 1 or any(distance > usable_range for distance in legs):
            raise FuelCoverageError("The selected fuel stops cannot safely cover the routed trip.")

        initial_gallons = legs[0] / miles_per_gallon
        initial_cost = initial_gallons * origin["price"]
        total_cost = initial_cost
        distance_from_start = legs[0]
        response_stops = []
        for index, stop in enumerate(stops):
            gallons = legs[index + 1] / miles_per_gallon
            cost = gallons * stop["price"]
            total_cost += cost
            response_stops.append(
                {
                    "station_name": stop["name"],
                    "address": stop["address"],
                    "city": stop["city"],
                    "state": stop["state"],
                    "price_per_gallon": round(stop["price"], 4),
                    "distance_from_start_miles": round(distance_from_start, 2),
                    "gallons_pumped": round(gallons, 2),
                    "cost": round(cost, 2),
                    "coordinates": [stop["latitude"], stop["longitude"]],
                }
            )
            distance_from_start += legs[index + 1]

        total_distance = float(routed_trip["distance_miles"])
        return {
            "stops": response_stops,
            "total_fuel_cost": round(total_cost, 2),
            "total_gallons_consumed": round(sum(legs) / miles_per_gallon, 2),
            "initial_fuel_cost": round(initial_cost, 2),
            "initial_fuel_price_per_gallon": round(origin["price"], 4),
            "initial_fuel_price_station": {
                "station_name": origin["name"],
                "city": origin["city"],
                "state": origin["state"],
            },
            "route_detour_miles": round(max(0, total_distance - baseline_distance), 2),
            "vehicle_range_miles": vehicle_range,
            "reserve_miles": reserve_miles,
        }

    @classmethod
    def optimize_route(
        cls,
        start_lat: float,
        start_lon: float,
        finish_lat: float,
        finish_lon: float,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Choose fuel stops from one local query and return the final routed trip."""
        baseline = cls.get_osrm_route(
            start_lat,
            start_lon,
            finish_lat,
            finish_lon,
            annotations=True,
        )
        if baseline is None:
            return None, None

        candidates = cls._station_candidates(baseline)
        if not candidates:
            raise FuelCoverageError("No geocoded fuel-price stations were found near this route.")
        origin, stops = cls._select_stops(baseline, candidates)
        routed_trip = baseline
        if stops:
            routed_trip = cls.get_osrm_route(
                start_lat,
                start_lon,
                finish_lat,
                finish_lon,
                via=[(stop["latitude"], stop["longitude"]) for stop in stops],
            )
            if routed_trip is None:
                raise UpstreamServiceError("The routing provider could not route through the selected fuel stops.")

        return routed_trip, cls._fuel_plan(origin, stops, float(baseline["distance_miles"]), routed_trip)
