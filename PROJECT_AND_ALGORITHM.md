# Project and Algorithm

## Request Path

1. Validate the two U.S. location strings.
2. Resolve them through Nominatim, with successful results cached for seven days.
3. Call OSRM once for the baseline driving route with road-distance annotations.
4. Read nearby, coordinate-enriched fuel stations in one database query.
5. Match each station to its closest route segment and choose a feasible low-cost stop sequence.
6. When stops are required, call OSRM once more through those stations. That final route drives the map, range check, miles, and fuel cost.

A short trip therefore uses one OSRM route call. A trip that needs fuel stops uses two. There are never per-station routing calls.

## Station Matching

OSRM returns a full route geometry and the road distance for every geometry segment. The service builds a small in-memory grid of those segments. Each station is compared only with nearby segments, then projected onto the closest segment in local miles.

This gives an exact progress position along OSRM's road-distance model instead of assigning the station to a coarse route sample. Stations farther than `FUEL_STATION_CORRIDOR_MILES` from the route are ignored; the default is 4 miles.

## Fuel Choice

The usable range is:

$$
\text{usable range} = \text{vehicle range} - \text{reserve}
$$

By default, that is $500 - 50 = 450$ miles. The optimizer first marks stations that can still reach the destination through another feasible station. It then applies the standard fuel-buying rule along the ordered route:

- Stop at the first reachable station that is cheaper than the current fuel source.
- Otherwise stop at the cheapest reachable station.

This rule minimizes fuel cost for the ordered candidate set while preserving the reserve constraint. Candidate access distance is used only to rank and filter nearby stations. The selected sequence is then routed by OSRM, so the returned map, leg distances, total gallons, and total cost use actual driving distance.

## Cost Model

Fuel consumption is calculated from final routed miles:

$$
\text{gallons} = \frac{\text{final route miles}}{10\ \text{MPG}}
$$

The first leg uses the nearest available origin-area station price. Every selected stop prices the following routed leg. The response exposes the origin-price assumption and every stop's planned gallons and cost.

## Data Requirement

The supplied OPIS CSV contains price and address data but no coordinates. The loader imports those rows safely, but they cannot be used for routing until coordinates are present. For a small one-time load, `--geocode-missing` enriches missing rows serially. For a production feed, use a coordinate-enriched source or a self-hosted/licensed geocoder.

## Performance Characteristics

- Two geocodes and one OSRM route call on an uncached short trip.
- A second OSRM route call only when stops are selected.
- One station query per request.
- Route and geocode cache hits avoid external calls.
- Candidate projection is local CPU work proportional to nearby route segments, not all route points times all stations.

## Operational Limits

Public Nominatim and OSRM endpoints are appropriate for development and light traffic only. Set an identifying Nominatim User-Agent and contact email. For sustained traffic, move those URLs to self-hosted or contracted services and use PostgreSQL/PostGIS once the station dataset outgrows SQLite.