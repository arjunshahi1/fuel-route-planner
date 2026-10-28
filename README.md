# Fuel Route Planner API

A Django REST API that takes a start and finish location in the USA and returns:

- the driving route, as GeoJSON and as an interactive map page;
- the most cost-effective fuel stops along it, for a vehicle with a **500-mile range** at **10 MPG**;
- the **total money spent on fuel**.

Fuel prices come from an OPIS truck-stop price file (`data/fuel-prices.csv`). Routing uses one call to a free routing API, and routes and geocoded addresses are cached, so a repeat request for the same trip makes no external calls.

```
POST /api/v1/route-plans/   {"start": "Chicago, IL", "finish": "Denver, CO"}
→ 3 fuel stops, $292.46 total, 1002.7 miles, interactive map link
```

---

## Quick start

Requires Python 3.12+ (Django 6.1).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_fuel_stations   # loads 6,626 geocoded US stations (~2 s)
python manage.py runserver
```

Then open http://127.0.0.1:8000/api/v1/ or import `postman/fuel-route-planner.postman_collection.json` into Postman.

No API key is needed. The default router is the public [OSRM](https://project-osrm.org/) server. To use [OpenRouteService](https://openrouteservice.org/) instead, copy `.env.example` to `.env` and set `ORS_API_KEY`.

Run the tests with:

```bash
python manage.py test fuel
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
    "total_fuel_cost": 292.46,
    "total_gallons": 100.27,
    "number_of_stops": 3,
    "average_price_per_gallon": 2.917,
    "stations_in_corridor": 202,
    "stop_penalty": 5.0,
    "cheapest_possible": {"total_fuel_cost": 291.58, "number_of_stops": 7}
  },
  "fuel_stops": [
    {
      "stop_number": 1, "opis_id": 70180, "name": "Gulf", "address": "SR-83",
      "city": "Bensenville", "state": "IL", "lat": 41.95801, "lng": -87.94383,
      "mile_marker": 14.6, "off_route_miles": 6.1, "price_per_gallon": 3.059,
      "fuel_on_arrival_gallons": 0.0, "gallons_purchased": 20.254, "lead_in_gallons": 1.459,
      "cost": 61.96
    }
  ],
  "map_url": "http://127.0.0.1:8000/api/v1/route-plans/7d04e6e2-.../map/",
  "map": {"type": "FeatureCollection", "features": ["route LineString, start, finish, one Point per stop"]},
  "assumptions": ["..."],
  "meta": {"external_api_calls": {"routing": 1, "geocoding": 0}, "route_cache_hit": false, "processing_time_ms": 945.4}
}
```

- `map_url` is an interactive Leaflet/OpenStreetMap page showing the route, the numbered fuel stops, and a cost summary. It's built from the stored plan, so opening it never calls the routing API.
- `map` is the same content as GeoJSON. You can paste it into [geojson.io](https://geojson.io).
- `summary.cheapest_possible` shows what the absolute cheapest plan would cost and how many stops it would take, so the stop-count trade-off is visible.

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
| 422 | No drivable route, or the route has a stretch longer than 500 miles with no known station. For example, Portland → San Diego: the data has almost no California stations |
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

**A practical plan.** The pure minimum is often impractical. Coast to coast it makes 17 stops, some only 13 miles apart to buy 1–2 gallons for a few cents of savings. So the planner minimizes **fuel cost + `stop_penalty` per stop** (default $5) with a dynamic program over (station, fuel level) at 0.1-mile resolution:
- the DP decides **where** to stop;
- the exact greedy then computes **how many gallons** to buy, using only those stations.

The test suite checks the DP against a **brute-force search over every subset of stations**.

| Route | Cheapest possible | Default (`stop_penalty=5`) |
|---|---|---|
| Chicago → Denver (1,003 mi) | 7 stops, $291.58 | 3 stops, $292.46 |
| New York → Los Angeles (2,810 mi) | 17 stops, $852.48 | 7 stops, $860.16 |
| Seattle → Miami (3,303 mi) | 20 stops, $1,026.91 | 9 stops, $1,033.88 |
| Boston → Washington DC (439 mi) | 8 stops, $146.37 | 3 stops, $148.76 |

Pass `"stop_penalty": 0` for the absolute cheapest plan.

### Performance

Measured locally against the public OSRM server:

| | Time |
|---|---|
| First request for a trip (one OSRM call) | ~1–3 s, almost all of it waiting on the public OSRM server |
| Our own processing (corridor + optimization + response) | ~20–40 ms |
| Repeat request (cached route) | ~20–50 ms |

---

## Assumptions

Every response also lists these in its `assumptions` field.

- **Vehicle:** 500-mile range, 10 MPG, so a 50-gallon tank.
- **Start:** the trip starts with an **empty tank**. The first fill-up is at a station within 25 miles of the start along the route. The fuel used to drive there is charged at that station's price and reported as `lead_in_gallons`.
- **Finish:** the vehicle arrives with an empty tank, so total gallons always equals distance ÷ MPG. Total cost is the sum of what is paid at each stop.
- **Stops:** a stop is only made when it saves more than `stop_penalty`. `summary.cheapest_possible` shows the alternative.
- **Station locations:** approximated by their city. `off_route_miles` is therefore approximate, and the detour to reach a station isn't added to fuel use.
- **Duplicate prices:** stations listed several times use their lowest price.
- **Coverage:** the contiguous USA only. Canadian rows in the file are ignored, and the price data has no stations in Alaska or Hawaii.

## Limitations

- The public OSRM server is a free demo service with a fair-use policy. For production, use OpenRouteService with a key, or a self-hosted OSRM.
- City-level geocoding is accurate enough for route planning, but not for turn-by-turn directions to a pump.
- The price file barely covers California: it has a handful of stations near the Mexican border. Routes inside California may correctly return 422.
- Plans are stored in SQLite; routes, geocoded addresses and rate-limit counters are kept in each process's memory. A multi-process deployment would use Postgres and a shared cache such as Redis.

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
  tests/                        64 tests: optimizer vs LP and brute force, API, loader, geo
data/
  fuel-prices.csv               OPIS station price file
  station_geocodes.csv          generated coordinates per station
  us_places.csv                 generated gazetteer for resolving input
postman/                        Postman collection
```
