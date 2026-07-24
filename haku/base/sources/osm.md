# OpenStreetMap (geocoding, routing, place lookup)

Reference/lookup, not an operator-owned data source: no mail, calendar, or stock to
mine here, just public map data on demand. Use it whenever grounding a location,
address, or travel-time estimate would sharpen a suggestion — geocode an address
mentioned in mail or Tana, check how far a candidate meeting spot is, or get
turn-by-turn directions for something on the operator's schedule.

## Reaching it

haku-console proxies a remote `osm` MCP server (`osmmcp`, wrapping public
Nominatim/OSRM/Overpass APIs — it holds no secret of its own and isn't reachable
except from haku-console's namespace). Every tool is a pure read over public map
data, so the reviewed console policy **auto-approves the whole surface** — no
approval-queue noise, unlike Grocy/Gmail's partial allowlists.

Reach it however your runtime wires it: managed sessions expose the tools directly as
in-session MCP tools; otherwise call `https://haku.allegedly.works/mcp` over MCP-HTTP
(<mcp_over_http.md>) with the `haku-console-agent-api` bearer from `haku-sandbox`,
same as the Gmail/Calendar console tools. Tool names get an `osm__` prefix, e.g.:

```bash
TOKEN=$(kubectl -n haku-sandbox get secret haku-console-agent-api -o jsonpath='{.data.token}' | base64 -d)
fastmcp call https://haku.allegedly.works/mcp osm__geocode_address \
  --input-json '{"address":"1600 Amphitheatre Parkway, Mountain View, CA"}' \
  --auth "$TOKEN" --transport http
```

## What to use

- **`geocode_address`** / **`reverse_geocode`** — address ↔ coordinates.
- **`route_fetch`** / **`get_route_directions`** / **`route_sample`** — OSRM routing:
  distance, duration, turn-by-turn steps, or sampled points along a route. `mode` is
  `car`, `bike`, or `foot` (not `profile`/`driving`).
- **`find_nearby_places`**, **`explore_area`**, **`find_parking_facilities`**,
  **`find_charging_stations`**, **`find_schools_nearby`** — POI search around a point.
  **`suggest_meeting_point`** and **`analyze_commute`** compare multiple locations
  (e.g. "where should we meet" / "how does this commute look by car vs. transit").
  **`analyze_neighborhood`** gives a livability summary for an area.
- **`geo_distance`**, **`bbox_from_points`**, **`centroid_points`**, **`sort_by_distance`**,
  **`polyline_encode`/`polyline_decode`**, **`enrich_emissions`** — small geometry/format
  utilities for composing the above.
- **`osm_query_bbox`** / **`filter_tags`** — raw Overpass queries against a bounding box,
  for anything the higher-level tools don't cover (remember the `osm__` MCP-tool prefix
  makes the query tool itself `osm__osm_query_bbox`).
- **`get_map_image`** — a rendered map image, for visual context.

Nominatim/OSRM's own usage policies (rate limits, no bulk geocoding) are enforced
server-side by `osmmcp` itself — nothing extra to observe here.
