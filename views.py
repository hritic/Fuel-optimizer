import json
import logging
from html import escape
from urllib.parse import urlencode

from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RoutePlanRequestSerializer
from .services import (
    FuelCoverageError,
    RouteOptimizerService,
    UpstreamServiceError,
)


logger = logging.getLogger(__name__)


class HealthAPIView(APIView):
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except DatabaseError:
            return Response(
                {"status": "unavailable", "database": "unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({"status": "ok", "database": "ok"})


class RoutePlanMixin:
    @staticmethod
    def _json_for_script(value):
        return (
            json.dumps(value, separators=(",", ":"), ensure_ascii=True)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )

    @classmethod
    def _build_map_html(cls, route_info, start_location, finish_location, fuel_plan):
        route_geojson = cls._json_for_script(route_info["geojson"])
        start = cls._json_for_script(
            {
                "coordinates": route_info["coordinates"][0],
                "label": start_location,
                "initialFuelPrice": fuel_plan["initial_fuel_price_per_gallon"],
                "initialFuelStation": fuel_plan["initial_fuel_price_station"],
            }
        )
        finish = cls._json_for_script(
            {"coordinates": route_info["coordinates"][-1], "label": finish_location}
        )
        stops = cls._json_for_script(fuel_plan["stops"])
        summary = cls._json_for_script(
            {
                "distanceMiles": route_info["distance_miles"],
                "durationHours": route_info["duration_hours"],
                "totalFuelCost": fuel_plan["total_fuel_cost"],
                "totalGallons": fuel_plan["total_gallons_consumed"],
                "stopCount": len(fuel_plan["stops"]),
            }
        )

        return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        :root {{ color-scheme: light; }}
        html, body, #map {{ height: 100%; width: 100%; margin: 0; }}
        body {{ font-family: Georgia, "Times New Roman", serif; }}
        #map {{ background: #dce8ea; }}
        .trip-card {{
            background: rgba(255, 255, 255, .96);
            border: 1px solid #cbd6d8;
            border-radius: 6px;
            box-shadow: 0 3px 14px rgba(27, 45, 50, .2);
            color: #172a2e;
            margin: 12px;
            max-width: 290px;
            padding: 12px;
        }}
        .trip-card__title {{ font-size: 15px; font-weight: 700; line-height: 1.3; margin: 0 0 10px; }}
        .trip-card__metrics {{ display: grid; gap: 7px; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        .trip-card__metric {{ border-top: 1px solid #e2e8e8; padding-top: 6px; }}
        .trip-card__label {{ color: #5d7074; display: block; font-size: 11px; text-transform: uppercase; }}
        .trip-card__value {{ display: block; font-family: Georgia, "Times New Roman", serif; font-size: 15px; font-weight: 700; margin-top: 2px; }}
        .route-marker {{ background: transparent; border: 0; }}
        .flag {{ filter: drop-shadow(0 2px 2px rgba(0, 0, 0, .4)); position: relative; }}
        .flag svg {{ display: block; }}
        .flag__label {{
            color: #ffffff;
            font: 700 12px Georgia, serif;
            left: 6px;
            position: absolute;
            text-align: center;
            top: 4px;
            width: 22px;
        }}
        .flag__tag {{
            background: #ffffff;
            border-radius: 3px;
            color: #172a2e;
            font: 700 10px Georgia, serif;
            left: 30px;
            padding: 1px 4px;
            position: absolute;
            top: 24px;
            white-space: nowrap;
        }}
        #map-error {{
            background: #ffffff;
            border: 1px solid #c13d31;
            color: #76251e;
            left: 50%;
            max-width: 360px;
            padding: 14px;
            position: absolute;
            text-align: center;
            top: 50%;
            transform: translate(-50%, -50%);
            z-index: 1000;
        }}
    </style>
</head>
<body>
    <div id="map"></div>
    <div id="map-error" hidden>Unable to load the map library. Check the browser network connection and reload.</div>
    <script>
        const routeGeoJson = {route_geojson};
        const start = {start};
        const finish = {finish};
        const stops = {stops};
        const summary = {summary};

        function escapeHtml(value) {{
            return String(value).replace(/[&<>"']/g, function(character) {{
                return {{"&": "&amp;", "<": "&lt;", ">": "&gt;", "\\"": "&quot;", "'": "&#39;"}}[character];
            }});
        }}

        if (!window.L) {{
            document.getElementById("map-error").hidden = false;
        }} else {{
        const map = L.map("map", {{zoomControl: false}});
        L.control.zoom({{position: "bottomright"}}).addTo(map);
        L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
            maxZoom: 19,
            attribution: "&copy; OpenStreetMap contributors"
        }}).addTo(map);
        L.control.scale({{imperial: true, metric: false, position: "bottomleft"}}).addTo(map);
        map.attributionControl.setPrefix(false);

        const routeCasing = L.geoJSON(routeGeoJson, {{
            style: {{color: "#ffffff", opacity: .9, weight: 9}}
        }}).addTo(map);
        const routeLine = L.geoJSON(routeGeoJson, {{
            style: {{color: "#0c6b78", opacity: .95, weight: 5}}
        }}).addTo(map);
        const endpoints = L.layerGroup().addTo(map);
        const fuelStops = L.layerGroup().addTo(map);

        const FLAG_COLORS = {{start: "#1f9d55", finish: "#a63d2d", fuel: "#b7791f"}};
        function markerIcon(kind, label, tag) {{
            const fill = kind === "finish"
                ? '<path d="M4 2h22v14H4z" fill="#ffffff" stroke="#172a2e" stroke-width="1"/>'
                    + '<path d="M4 2h5.5v4.67H4zM15 2h5.5v4.67H15zM9.5 6.67H15v4.66H9.5zM20.5 6.67H26v4.66h-5.5zM4 11.33h5.5V16H4zM15 11.33h5.5V16H15z" fill="#172a2e"/>'
                : '<path d="M4 2h22l-5 7 5 7H4z" fill="' + FLAG_COLORS[kind] + '" stroke="#ffffff" stroke-width="1.5"/>';
            const text = kind === "finish" ? "" : '<span class="flag__label">' + escapeHtml(label) + "</span>";
            const tagHtml = tag ? '<span class="flag__tag">' + escapeHtml(tag) + "</span>" : "";
            return L.divIcon({{
                className: "route-marker",
                html: '<div class="flag"><svg width="30" height="44" viewBox="0 0 30 44">'
                    + '<rect x="2" y="1" width="2.5" height="42" fill="#172a2e"/>' + fill + "</svg>" + text + tagHtml + "</div>",
                iconAnchor: [3, 43],
                iconSize: [30, 44],
                popupAnchor: [10, -40]
            }});
        }}

        const initialFuel = "<br>Initial fuel estimate: $" + escapeHtml(start.initialFuelPrice)
            + "/gal at " + escapeHtml(start.initialFuelStation.station_name);
        L.marker(start.coordinates, {{icon: markerIcon("start", "S", "Start"), zIndexOffset: 1000}})
            .addTo(endpoints)
            .bindPopup("<strong>Start</strong><br>" + escapeHtml(start.label) + initialFuel);
        L.marker(finish.coordinates, {{icon: markerIcon("finish", "F", "Finish"), zIndexOffset: 1000}})
            .addTo(endpoints)
            .bindPopup("<strong>Finish</strong><br>" + escapeHtml(finish.label));

        stops.forEach(function(stop, index) {{
            const popup = "<strong>Fuel stop " + (index + 1) + ":</strong> " + escapeHtml(stop.station_name)
                + "<br>" + escapeHtml(stop.city) + ", " + escapeHtml(stop.state)
                + "<br>Price: $" + escapeHtml(stop.price_per_gallon) + "/gal"
                + "<br>Planned gallons: " + escapeHtml(stop.gallons_pumped)
                + "<br>Estimated cost: $" + escapeHtml(stop.cost);
            L.marker(stop.coordinates, {{icon: markerIcon("fuel", String(index + 1), "Stop " + (index + 1))}})
                .addTo(fuelStops)
                .bindPopup(popup);
        }});

        const TripSummary = L.Control.extend({{
            options: {{position: "topright"}},
            onAdd: function() {{
                const panel = L.DomUtil.create("section", "trip-card");
                L.DomEvent.disableClickPropagation(panel);
                function metric(label, value) {{
                    return '<div class="trip-card__metric"><span class="trip-card__label">' + label
                        + '</span><span class="trip-card__value">' + escapeHtml(value) + "</span></div>";
                }}
                panel.innerHTML = '<h1 class="trip-card__title"></h1><div class="trip-card__metrics">'
                    + metric("Distance", summary.distanceMiles + " mi")
                    + metric("Drive time", summary.durationHours + " hr")
                    + metric("Fuel estimate", "$" + summary.totalFuelCost)
                    + metric("Fuel stops", summary.stopCount)
                    + "</div>";
                panel.querySelector("h1").textContent = start.label + " to " + finish.label;
                return panel;
            }}
        }});
        map.addControl(new TripSummary());
        L.control.layers(null, {{"Route": L.layerGroup([routeCasing, routeLine]), "Start / finish flags": endpoints, "Fuel stop flags": fuelStops}}, {{collapsed: false}}).addTo(map);

        const bounds = routeLine.getBounds();
        if (bounds.isValid()) {{
            map.fitBounds(bounds, {{padding: [32, 32]}});
        }}
        }}
    </script>
</body>
</html>"""

    @staticmethod
    def _build_map_error_html(message):
        return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        html, body {{ height: 100%; margin: 0; }}
        body {{ align-items: center; background: #dce8ea; color: #172a2e; display: flex; font-family: Georgia, "Times New Roman", serif; justify-content: center; }}
        .map-error {{ background: #ffffff; border: 1px solid #cbd6d8; border-left: 5px solid #a63d2d; box-shadow: 0 3px 14px rgba(27, 45, 50, .2); max-width: 430px; padding: 22px; }}
        h1 {{ font-size: 19px; margin: 0 0 9px; }}
        p {{ color: #53686c; line-height: 1.45; margin: 0; }}
    </style>
</head>
<body>
    <main class="map-error">
        <h1>Route map unavailable</h1>
        <p>{escape(str(message))}</p>
    </main>
</body>
</html>"""

    def _plan_route(self, data):
        request_serializer = RoutePlanRequestSerializer(data=data)
        if not request_serializer.is_valid():
            return None, Response(
                {
                    "error": "Please provide both start_location and finish_location parameters.",
                    "details": request_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_location = request_serializer.validated_data["start_location"]
        finish_location = request_serializer.validated_data["finish_location"]

        try:
            start_lat, start_lon = RouteOptimizerService.geocode_location(start_location)
            finish_lat, finish_lon = RouteOptimizerService.geocode_location(finish_location)
        except UpstreamServiceError as exc:
            logger.warning("route_plan_geocoding_failed error=%s", exc)
            return None, Response(
                {"error": "The location service is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if None in [start_lat, start_lon, finish_lat, finish_lon]:
            return None, Response(
                {"error": "Could not geocode one or both provided US locations."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            route_info, fuel_plan = RouteOptimizerService.optimize_route(
                start_lat,
                start_lon,
                finish_lat,
                finish_lon,
            )
        except UpstreamServiceError as exc:
            logger.warning("route_plan_routing_failed error=%s", exc)
            return None, Response(
                {"error": "The routing service is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except FuelCoverageError as exc:
            return None, Response(
                {"error": str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if not route_info:
            return None, Response(
                {"error": "Failed to calculate a valid driving route between inputs."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return {
            "start_location": start_location,
            "finish_location": finish_location,
            "route_info": route_info,
            "fuel_plan": fuel_plan,
        }, None

    @staticmethod
    def _map_url(start_location, finish_location):
        return f"{reverse('plan-route-map')}?{urlencode({
            'start_location': start_location,
            'finish_location': finish_location,
        })}"


class PlanRouteAPIView(RoutePlanMixin, APIView):
    def post(self, request):
        plan, error_response = self._plan_route(request.data)
        if error_response:
            return error_response

        return Response({
            "route_summary": {
                "start": plan["start_location"],
                "finish": plan["finish_location"],
                "total_distance_miles": plan["route_info"]["distance_miles"],
                "duration_hours": plan["route_info"]["duration_hours"],
            },
            "fuel_optimization": {
                **plan["fuel_plan"],
                "number_of_fuel_stops": len(plan["fuel_plan"]["stops"]),
            },
            "map_url": self._map_url(plan["start_location"], plan["finish_location"]),
        }, status=status.HTTP_200_OK)


class PlanRouteMapAPIView(RoutePlanMixin, APIView):
    def get(self, request):
        plan, error_response = self._plan_route(request.query_params)
        if error_response:
            return HttpResponse(
                self._build_map_error_html(error_response.data.get("error", "Unable to build route map.")),
                content_type="text/html; charset=utf-8",
                status=error_response.status_code,
            )

        response = HttpResponse(
            self._build_map_html(
                plan["route_info"],
                plan["start_location"],
                plan["finish_location"],
                plan["fuel_plan"],
            ),
            content_type="text/html; charset=utf-8",
        )
        # OSM tile servers reject requests without a Referer; Django's default is same-origin.
        response["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response
