# Fuel Route Planner API

A Django REST API that takes a start and finish location in the USA and returns:

- the driving route, as GeoJSON and as an interactive map page;
- the most cost-effective fuel stops along it, for a vehicle with a **500-mile range** at **10 MPG**;
- the **total money spent on fuel**.

Fuel prices come from an OPIS truck-stop price file (`data/fuel-prices.csv`). Routing uses one call to a free routing API, and routes and geocoded addresses are cached, so a repeat request for the same trip makes no external calls.

```
POST /api/v1/route-plans/   {"start": "Chicago, IL", "finish": "Denver, CO"}
→ 3 fuel stops, $297.45 total, 1002.7 miles, interactive map link
```

---

## Quick start

**With Docker** (no Python needed):

```bash
docker build -t fuel-route-planner .
docker run -p 8000:8000 fuel-route-planner
```

**Without Docker** (Python 3.12+, required by Django 6.1):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # optional: enables DEBUG for local development
python manage.py migrate
python manage.py load_fuel_stations   # loads 6,626 geocoded US stations (~2 s)
python manage.py runserver
```

Then open http://127.0.0.1:8000/api/v1/ or import `postman/fuel-route-planner.postman_collection.json` into Postman.

After re-running `load_fuel_stations` (for example with a newer price file), restart the server: each server process keeps the stations in memory.

No API key is needed. The default router is the public [OSRM](https://project-osrm.org/) server. To use [OpenRouteService](https://openrouteservice.org/) instead, copy `.env.example` to `.env` and set `ORS_API_KEY`.

Run the tests and the linter with:

```bash
python manage.py test fuel
flake8   # configured in .flake8 (120-character lines)
```

---

## API

### `POST /api/v1/route-plans/`

| Field | Type | Description |
|---|---|---|
| `start`, `finish` | string or object | `"Chicago, IL"`, `"Chicago, Illinois"`, `"41.88,-87.63"`, `{"lat": 41.88, "lng": -87.63}`, or a street address |
| `stop_penalty` | number, optional | Dollars a stop must save to be worth making (default `5`). Use `0` for the absolute cheapest plan |

A successful request returns **`201 Created`**: the plan is saved, and the `Location` header points to it. Requests are rate-limited per client (default 60/minute, `API_RATE_LIMIT`).

**Response (trimmed):**

```json
{
  "id": "7d04e6e2-d8ac-441a-a926-71f3fc275652",
  "start":  {"query": "Chicago, IL", "label": "Chicago, IL", "lat": 41.83705, "lng": -87.68494, "resolved_by": "gazetteer"},
  "finish": {"query": "Denver, CO",  "label": "Denver, CO",  "lat": 39.76185, "lng": -104.88111, "resolved_by": "gazetteer"},
  "route":   {"distance_miles": 1002.7, "duration_hours": 17.79, "provider": "osrm"},
  "vehicle": {"range_miles": 500.0, "mpg": 10.0, "tank_capacity_gallons": 50.0},
  "summary": {
    "total_fuel_cost": 297.45,
    "total_gallons_purchased": 101.63,
    "starting_fuel_gallons": 0.0,
    "fuel_used_gallons": 101.63,
    "number_of_stops": 3,
    "average_price_per_gallon": 2.927,
    "stations_in_corridor": 202,
    "stop_penalty": 5.0
  },
  "fuel_stops": [
    {
      "stop_number": 1, "opis_id": 72438, "name": "QUIKTRIP #7208", "address": "I-290 EXIT 18A",
      "city": "Bellwood", "state": "IL", "lat": 41.8829, "lng": -87.87617,
      "mile_marker": 12.8, "off_route_miles": 0.8, "price_per_gallon": 3.079,
      "fuel_on_arrival_gallons": 0.0, "gallons_purchased": 32.473,
      "lead_in_gallons": 1.368, "detour_gallons": 0.083, "cost": 99.99
    }
  ],
  "cheapest_possible": {"total_fuel_cost": 297.23, "number_of_stops": 4, "fuel_stops": ["..."]},
  "map_url": "http://127.0.0.1:8000/api/v1/route-plans/7d04e6e2-.../map/",
  "map": {"type": "FeatureCollection", "features": ["route LineString, start, finish, one Point per stop"]},
  "assumptions": ["..."],
  "meta": {"external_api_calls": {"routing": 1, "geocoding": 0}, "route_cache_hit": false, "processing_time_ms": 945.4}
}
```

- `map_url` is an interactive Leaflet/OpenStreetMap page showing the route, the numbered fuel stops, and a cost summary. It's built from the stored plan, so opening it never calls the routing API.
- `map` is the same content as GeoJSON. You can paste it into [geojson.io](https://geojson.io).
- `cheapest_possible` is the absolute cheapest plan (no stop penalty), with its own `fuel_stops`, so the trade-off between cost and number of stops is visible.
- `total_gallons_purchased` is what is paid for. `fuel_used_gallons` is what the trip burns (distance ÷ MPG plus detours); the two differ only when the trip starts with a full tank (`starting_fuel_gallons`, see Assumptions).

### Other endpoints

| Endpoint | Description |
|---|---|
| `GET /api/v1/route-plans/<id>/` | A previously computed plan |
| `GET /api/v1/route-plans/<id>/map/` | Interactive map page |
| `GET /api/v1/` | API index |

### Errors

API errors have the shape `{"error": {"message": "...", "field": "start"}}`. The HTML map page returns a plain 404 for an unknown plan.

| Status | When |
|---|---|
| 400 | Missing or invalid input, a location outside the contiguous USA (`"Toronto, ON"`, `"Anchorage, AK"`), or an unknown place |
| 405 | GET, PUT, PATCH or DELETE on `/api/v1/route-plans/` (only POST and OPTIONS are allowed) |
| 422 | No drivable route, or the route has a stretch longer than 500 miles with no known station. For example, Portland → San Diego (991 miles without a station: the data has almost no California stations) |
| 429 | Rate limit exceeded |
| 500 | Misconfiguration, e.g. `ROUTING_PROVIDER=ors` without `ORS_API_KEY` |
| 502/504 | The routing provider failed or timed out |
| 503 | No stations loaded yet (run `load_fuel_stations`). No routing call is made |

---

## How it works

```
start/finish ──► resolve locations ──► 1 routing call ──► stations in a corridor ──► optimise stops ──► JSON + map
                 (local gazetteer;     (OSRM or ORS;       (KD-tree, ~10 ms)          (DP + greedy, ~20 ms)
                  no API call for       cached per trip)
                  "City, ST")
```

### 1. Preparing the price data (offline, once)

The CSV has no coordinates. Its `Address` column holds highway descriptions such as `"I-44, EXIT 283 & US-69"`, which a geocoder can't place. Stations are therefore located by **city and state**, using free public gazetteers entirely offline:

1. US Census Gazetteer: incorporated places
2. US Census Gazetteer: county subdivisions, together with GeoNames populated places. These cover unincorporated truck-stop towns such as *Ruther Glen, VA*.

Names are normalized before matching, so "Mc Calla" matches "McCalla", "Saint Johns" matches "St. Johns", and "Cañon City" matches "Canon City".

When a name matches several places in the same state, the candidate closest to the station's **Rack ID** centroid wins. A rack is a regional wholesale fuel terminal, so stations that share one are clustered together. Five place names that no rule resolves correctly were checked by hand against their highway exits. Four got manual coordinates and one got an alias, all listed in `build_geocodes.py`.

**Result:** all 6,626 US stations are geocoded, with no unresolved rows.

`python manage.py build_geocodes` re-runs this step: it downloads about 70 MB of public data and writes `data/station_geocodes.csv` and `data/us_places.csv`. Both outputs are committed, so you don't need to run it.

`load_fuel_stations` then:
- drops 620 Canadian rows;
- deduplicates by OPIS ID, keeping the **lowest price**. The file lists some stations up to 6 times at different prices;
- keeps full 8-decimal price precision;
- repairs mis-encoded text such as `Stuckeyâ€™s`.

### 2. Resolving locations: usually 0 API calls

| Input | How it is resolved | API calls |
|---|---|---|
| `"City, ST"`, `"City, State"`, `"City, ST 12345"`, `"City, ST, USA"` | Local gazetteer (~35k US cities, towns and CDPs) | 0 |
| `"lat,lng"` or `{"lat", "lng"}` | Used directly, after checking it is within 30 miles of a known US place (an approximate border check) | 0 |
| A street address or landmark | One Nominatim (OpenStreetMap) request, restricted to the US and cached for a day | 1 (0 when cached) |

The `resolved_by` field in the response says which path was used: `gazetteer`, `coordinates`, `nominatim`, or `nominatim_cache`.

### 3. Routing: 1 API call

There is one request per trip to OSRM, or to OpenRouteService if `ORS_API_KEY` is set. It returns the full route geometry and the road distance. Routes are cached per (provider, start, finish), so a repeat request makes **no** routing call.

**Worst case:** 3 external calls, when both ends are street addresses. **Typical case:** 1.

### 4. Finding stations along the route

The route polyline (about 9k points for 1,000 miles) is densified to 0.5-mile steps and loaded into a **KD-tree**. Each station near the route's bounding box is matched to its nearest route point, which gives:
- its **mile marker** along the route;
- its **distance from the route**.

Stations within 10 miles are kept. The corridor is that wide because station coordinates are city-level. Station data is held in memory as numpy arrays, so requests never query the database for stations.

### 5. Choosing fuel stops

The vehicle has a 500-mile range at 10 MPG, so a 50-gallon tank.

**The cheapest plan.** This is the classic *gas station problem*, which a greedy algorithm solves exactly:
- if a cheaper station is within range, buy just enough to reach it;
- otherwise, if the destination is in range, buy just enough to finish;
- otherwise, fill up and go to the cheapest station in range.

The test suite checks this greedy against an independent **linear-programming solver** (`scipy.optimize.linprog`) on 400 random routes, and it matches to 6 decimal places.

**Real-world costs of a stop.** Two things make the pure minimum impractical, so each stop carries a cost:
- **Detours.** A station can be up to 10 miles off the route. The fuel for the round trip (`detour_gallons`, 2 × `off_route_miles` ÷ MPG) is bought at that station and counted in its cost, so a station 9 miles away that is 1¢ cheaper doesn't beat one on the route. The vehicle must reach the station with enough fuel for the drive out, and the tank must also hold the drive back, so filling up off the route leaves less than 500 miles of range.
- **Stop penalty.** Without it, plans include stops that save cents, such as buying 0.2 gallons because the next station is half a cent cheaper. The planner minimizes **fuel cost + detour fuel + `stop_penalty` per stop** (default $5).

This uses a dynamic program over (station, fuel level) at 0.1-mile resolution:
- the DP decides **where** to stop;
- the exact greedy then computes **how many gallons** to buy, using only those stations.

The test suite checks the DP against a **brute-force search over every subset of stations**.

| Route | Cheapest possible | Default (`stop_penalty=5`) |
|---|---|---|
| Chicago → Denver (1,003 mi) | 4 stops, $297.23 | 3 stops, $297.45 |
| New York → Los Angeles (2,810 mi) | 10 stops, $866.41 | 7 stops, $871.73 |
| Seattle → Miami (3,303 mi) | 14 stops, $1,035.09 | 9 stops, $1,040.58 |
| Boston → Washington DC (439 mi) | 4 stops, $150.07 | 3 stops, $151.47 |

Pass `"stop_penalty": 0` to get the cheapest plan as the main result.

### Performance

Measured locally against the public OSRM server:

| | Time |
|---|---|
| First request for a trip (one OSRM call) | ~1–3 s, almost all of it waiting on the public OSRM server |
| Our own processing (corridor + both plans + response) | ~30–90 ms |
| Repeat request (cached route) | ~40–90 ms |

The first request after a server starts also loads the station index into memory (~0.3 s, once per process).

---

## Assumptions

Every response also lists these in its `assumptions` field.

- **Vehicle:** 500-mile range, 10 MPG, so a 50-gallon tank.
- **Start:** the trip starts with an **empty tank**. The first fill-up is at a station within 25 miles of the start along the route. The fuel used to drive there is charged at that station's price and reported as `lead_in_gallons`.
- **Start without a nearby station:** the price data has no stations in some large cities (Houston, Los Angeles, San Francisco). The vehicle then starts with a **full tank**, which is not part of the cost (`starting_fuel_gallons`). Houston → Dallas (238 mi) therefore needs no purchase and costs $0.
- **Finish:** the vehicle arrives with an empty tank. Total cost is the sum of what is paid at each stop.
- **Stops:** a stop is only made when it saves more than `stop_penalty`. `cheapest_possible` shows the alternative.
- **Detours:** the fuel to reach a station off the route and return is included (`detour_gallons`). On the first stop the drive out to the pump is part of `lead_in_gallons`, and `detour_gallons` is the drive back. At every stop, `fuel_on_arrival_gallons` (measured at the pump) plus `gallons_purchased` minus `lead_in_gallons` is at most the 50-gallon tank.
- **Station locations:** approximated by their city, so `off_route_miles` and detours are approximate.
- **Duplicate prices:** stations listed several times use their lowest price.
- **Coverage:** the contiguous USA only. Canadian rows in the file are ignored, and the price data has no stations in Alaska or Hawaii.

## Limitations

- The public OSRM server is a free demo service with a fair-use policy. For production, use OpenRouteService with a key, or a self-hosted OSRM.
- City-level geocoding is accurate enough for route planning, but not for turn-by-turn directions to a pump.
- The price file barely covers California: it has a handful of stations near the Mexican border. Routes inside California may correctly return 422.
- Plans are stored in SQLite. Routes, geocoded addresses and rate-limit counters are kept in a file cache (`.cache/`), which survives restarts and is shared by the processes on one machine. A deployment across several machines would use Postgres and a shared cache such as Redis.
- `DEBUG` is off unless `DJANGO_DEBUG=true` is set (`.env.example` sets it for local development). A real deployment must also set `DJANGO_SECRET_KEY`.
- Stations are kept in each server process's memory, so a server needs a restart after `load_fuel_stations` runs.

---

## Project layout

```
config/                         Django settings and root URLs
fuel/
  models.py                     FuelStation, RoutePlan
  views.py, serializers.py      REST endpoints and map page
  services/
    places.py                   place-name normalization and local gazetteer
    geocoding.py                start/finish resolution
    routing.py                  OSRM and OpenRouteService clients, with caching
    corridor.py                 stations along a route (KD-tree)
    optimizer.py                greedy + stop-penalty DP
    planner.py                  ties it all together and builds the response
    stations.py                 in-memory station index
    geo.py                      geodesy helpers
  management/commands/
    build_geocodes.py           offline geocoding (run once; output committed)
    load_fuel_stations.py       CSV → database
  templates/fuel/map.html       Leaflet map
  tests/                        70 tests: optimizer vs LP and brute force, API, loader, geo
data/
  fuel-prices.csv               OPIS station price file
  station_geocodes.csv          generated coordinates per station
  us_places.csv                 generated gazetteer for resolving input
postman/                        Postman collection
Dockerfile                      gunicorn image with the stations pre-loaded
```
