# Fuel Route Optimizer API

A Django REST API that plans a U.S. driving route, identifies cost-oriented fuel stops from local fuel-price data, estimates fuel spend at 10 MPG, and returns a standalone interactive Leaflet map.

## What It Does

- Accepts a start and finish location within the United States.
- Resolves each location with Nominatim and obtains one driving route from OSRM.
- Uses one local station query and OSRM road-distance annotations to select reachable fuel stops for a 500-mile vehicle range.
- Re-routes through selected stops only when they are needed, so the returned map, distance, and fuel cost use final driving legs.
- Returns route distance, estimated fuel cost, station details, and safe HTML for an interactive map.
- Caches successful geocodes for seven days and routes for one day by default.

An uncached short trip uses two geocodes and one OSRM route call. A trip with fuel stops uses one additional OSRM route call; cache hits avoid repeated calls.

## Providers

The default development providers are:

- Nominatim: `https://nominatim.openstreetmap.org` for geocoding.
- OSRM demo service: `https://router.project-osrm.org` for driving routes.
- OpenStreetMap tile servers for the returned Leaflet map.

These are free public services, not production capacity guarantees. Public Nominatim requests must use an identifying User-Agent, stay below one request per second, be cached, and show OpenStreetMap attribution. The map includes attribution. Set `NOMINATIM_CONTACT_EMAIL` and replace both base URLs with self-hosted or contracted providers before serving material traffic.

## Quick Start

### Docker

1. Create a local `.env` from `.env.example` and set a long random `DJANGO_SECRET_KEY`.
2. Start the stack:

```powershell
docker compose up --build -d
```

3. Check liveness:

```powershell
Invoke-RestMethod http://localhost:8000/api/health/
```

4. Import the supplied fuel-price file:

```powershell
docker compose exec web python manage.py load_fuel_data data/fuel-prices-for-be-assessment.csv
```

For a quick route-test dataset, import and geocode only the first 50 valid unique records:

```powershell
docker compose exec web python manage.py load_fuel_data data/fuel-prices-for-be-assessment.csv --limit 50
```

The container runs migrations and collects static files on startup. Redis remains internal to the Compose network and backs the cache.

### Fuel Data Coordinates

The supplied OPIS CSV contains price, address, city, and state fields but no latitude or longitude columns. The import command now loads those records without deleting existing data, but an unresolved station cannot be selected by the route optimizer.

For a small, one-time import, coordinates can be enriched deliberately:

```powershell
docker compose exec web python manage.py load_fuel_data data/fuel-prices-for-be-assessment.csv --geocode-missing
```

That mode is single-threaded and never exceeds one request per second. Do not use the public Nominatim instance for a large or recurring enrichment job. Point `NOMINATIM_BASE_URL` to a self-hosted or licensed geocoder first, or load a coordinate-enriched source file.

## API

### Health

`GET /api/health/`

Returns `200` when Django can reach its database. It does not call external mapping providers.

```json
{
  "status": "ok",
  "database": "ok"
}
```

### Plan a Route

`POST /api/plan-route/`

```json
{
  "start_location": "Gilbert, SC",
  "finish_location": "Corinth, MS"
}
```

PowerShell example:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/plan-route/ `
  -ContentType 'application/json' `
  -Body '{"start_location":"Gilbert, SC","finish_location":"Corinth, MS"}'
```

Successful responses contain:

```json
{
  "route_summary": {
    "start": "Gilbert, SC",
    "finish": "Corinth, MS",
    "total_distance_miles": 503.15,
    "duration_hours": 9.7
  },
  "fuel_optimization": {
    "stops": [
      {
        "station_name": "TRUCK STOP 44",
        "address": "I-20 EXIT 44",
        "city": "Gilbert",
        "state": "SC",
        "price_per_gallon": 3.079,
        "distance_from_start_miles": 0.0,
        "gallons_pumped": 8.02,
        "cost": 24.7,
        "coordinates": [33.9243121, -81.3937132]
      },
      {
        "station_name": "CIRCLE K #2705567",
        "address": "I-20, EXIT 172",
        "city": "Thomson",
        "state": "GA",
        "price_per_gallon": 3.399,
        "distance_from_start_miles": 80.21,
        "gallons_pumped": 42.29,
        "cost": 143.76,
        "coordinates": [33.4663248, -82.5026089]
      }
    ],
    "total_fuel_cost": 168.45,
    "total_gallons_consumed": 50.32,
    "initial_fuel_cost": 0.0,
    "initial_fuel_price_per_gallon": 3.079,
    "initial_fuel_price_station": {
      "station_name": "TRUCK STOP 44",
      "city": "Gilbert",
      "state": "SC"
    },
    "route_detour_miles": 3.88,
    "vehicle_range_miles": 500.0,
    "reserve_miles": 50.0,
    "number_of_fuel_stops": 2
  },
  "map_url": "/api/plan-route/map/?start_location=Gilbert%2C+SC&finish_location=Corinth%2C+MS"
}
```

`map_url` serves the interactive Leaflet map as `text/html`. Open it directly or embed it with an iframe:

```html
<iframe
  src="/api/plan-route/map/?start_location=Gilbert%2C+SC&finish_location=Corinth%2C+MS"
  title="Route map"
  style="width: 100%; height: 640px; border: 0"
></iframe>
```

The map includes route and fuel-stop overlays, start and finish flags, a trip summary, and scale and zoom controls.

### Error Semantics

| Status  | Meaning                                                                                                                   |
| ------- | ------------------------------------------------------------------------------------------------------------------------- |
| `400` | Missing or invalid request fields.                                                                                        |
| `422` | A location is not resolvable in the U.S., no driving route exists, or local station coverage cannot safely span the trip. |
| `502` | Nominatim or OSRM is unavailable or malformed its response.                                                               |
| `503` | The health endpoint cannot reach the database.                                                                            |

## Fuel Assumptions

- Maximum tank range: 500 miles.
- Safety reserve: 50 miles.
- Fuel economy: 10 miles per gallon.
- The returned total uses the final OSRM route through the selected stations, including its actual driving detours.
- The vehicle is treated as departing with fuel. Consumption before the first planned stop is priced using a nearby route station, with the selected source reported as `initial_fuel_price_station`.
- Each reported fuel stop prices the fuel allocated to the following leg. This makes the total represent estimated fuel consumed by the trip rather than an arbitrary zero-dollar initial tank.

All three vehicle values are configurable through environment variables. See `.env.example`.

## Performance Model

- Geocodes and route geometries are cached independently.
- The service requests one annotated OSRM route, then a second route through selected stops only when the trip requires them.
- It fetches candidate stations with one bounded database query.
- A local segment grid projects nearby stations onto exact OSRM route progress without scanning every station against every route segment.
- The final route validates that every driving leg fits within the reserve-aware range.

See [PROJECT_AND_ALGORITHM.md](PROJECT_AND_ALGORITHM.md) for the architecture, algorithm detail, known limitations, and production roadmap.

## Development and Tests

The deployed image uses Python 3.12. To run the deterministic test suite in a compatible environment:

```powershell
python manage.py test route_planner
```

The tests mock public HTTP calls and cover geocoding request shape, OSRM annotations, exact station projection, final-route fuel cost, CSV parsing, map escaping, response status mapping, and the health endpoint.

## Production Checklist

- Set a unique `DJANGO_SECRET_KEY`; the application refuses to boot without it outside tests.
- Restrict `DJANGO_ALLOWED_HOSTS` to real hostnames.
- Use HTTPS at the ingress and configure secure cookies if browser sessions are enabled.
- Move from SQLite to PostgreSQL with PostGIS when station data or traffic grows.
- Run self-hosted or contracted geocoding/routing infrastructure for sustained traffic.
- Keep an auditable, coordinate-enriched fuel-price feed and refresh it independently of route requests.
- Add request rate limiting, structured logs, metrics, tracing, and alerts before public exposure.
