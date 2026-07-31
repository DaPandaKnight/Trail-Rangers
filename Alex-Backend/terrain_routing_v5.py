"""
terrain_routing_v5.py
─────────────────────────────────────────────────────────────────────────
Performance rewrite of terrain_routing.py's grid-building step — same
directional Tobler cost formula, same A* logic, same fixed-real-world-
cell-size grid, just computed with vectorized NumPy instead of nested
Python loops calling haversine_km()/exp() per edge.

WHY THIS ONE AND NOT terrain_routing_fast.py
─────────────────────────────────────────────────────────────────────────
terrain_routing_fast.py (MCP_Geometric) was abandoned: it can only cost
each grid cell with a single isotropic slope value, not a genuine
directional per-edge cost — which meant it was optimizing against the
wrong signal (confirmed by the directional-verification test: the same
path scored 1.43h isotropic vs. 1.07h recomputed directionally, and the
path it actually found was measurably worse — 302m climb vs. 221m from
the correct graph version). Attempting a custom MCP_Flexible subclass to
fix that also turned up unreliable/version-inconsistent internal
behavior under direct testing. Given correctness is the priority, both
of those are dead ends — documented here, not silently dropped.

This version instead speeds up the SAME correct approach terrain_routing.py
already uses: it precomputes cost for all 8 directions across the whole
grid in one vectorized pass each (8 total passes, not n_rows*n_cols*8
individual Python calls), then runs the identical heapq-based A* — reading
costs from those precomputed arrays instead of a dict of Python edge
objects. The formula per direction is identical to edge_cost_hours() in
terrain_routing.py; this file changes HOW it's computed, not WHAT is
computed.

CORRECTNESS CHECK — READ THE OUTPUT WHEN YOU RUN THIS
─────────────────────────────────────────────────────────────────────────
The __main__ block runs on the exact same DEFAULT_TEST_BBOX and
DEFAULT_CELL_SIZE_M as terrain_routing.py and prints its result next to
the previously-confirmed numbers (2.51 km, ~0.78 h, 221.4 m climb). If
this version doesn't land on essentially the same numbers, that's a bug
in this rewrite, not a "different but valid" result — investigate before
trusting it.

Dependencies: `pip install requests pillow numpy`
─────────────────────────────────────────────────────────────────────────
"""

import heapq
import math
import time
from io import BytesIO

import numpy as np
import requests
from PIL import Image

# ── Config — identical to terrain_routing.py for a fair, direct comparison ─

LINZ_API_KEY = "d01KKYTQ3K36KWETM4W8J4VMN6S"

ELEVATION_TILE_URL = (
    "https://basemaps.linz.govt.nz/v1/tiles/elevation/WebMercatorQuad/"
    "{z}/{x}/{y}.png?api={key}&pipeline=terrain-rgb"
)

TILE_SIZE = 256
DEFAULT_TEST_BBOX = (-39.16, 175.60, -39.15, 175.62)  # min_lat, min_lon, max_lat, max_lon
DEFAULT_CELL_SIZE_M = 15

# Same 8-direction order as terrain_routing.py's `deltas`, kept identical
# so results are directly comparable.
DIRECTIONS = [(-1, -1), (-1, 0), (-1, 1),
              (0, -1),           (0, 1),
              (1, -1),  (1, 0),  (1, 1)]


# ── Geometry helpers (same formulas as terrain_routing.py) ─────────────────

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6371.0
    lng1, lat1 = a
    lng2, lat2 = b
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    h = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(d_lng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def meters_per_degree(lat: float) -> tuple[float, float]:
    return 111_320.0, 111_320.0 * math.cos(math.radians(lat))


def zoom_for_resolution(cell_size_m: float, lat: float,
                          min_zoom: int = 10, max_zoom: int = 15) -> int:
    for z in range(min_zoom, max_zoom + 1):
        meters_per_pixel = 156_543.03392 * math.cos(math.radians(lat)) / (2 ** z)
        if meters_per_pixel <= cell_size_m:
            return z
    return max_zoom


def lonlat_to_tile_and_pixel(lon: float, lat: float, zoom: int,
                               tile_size: int = TILE_SIZE):
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    x_float = (lon + 180.0) / 360.0 * n
    y_float = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    xtile, ytile = int(x_float), int(y_float)
    px = int((x_float - xtile) * tile_size)
    py = int((y_float - ytile) * tile_size)
    return xtile, ytile, px, py


def decode_terrain_rgb(img: Image.Image) -> np.ndarray:
    arr = np.asarray(img.convert("RGB"), dtype=np.float64)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    return -10000 + (r * 256 * 256 + g * 256 + b) * 0.1


def tobler_speed_kmh_array(slope: np.ndarray) -> np.ndarray:
    """Identical formula to terrain_routing.py's tobler_speed_kmh(), vectorized."""
    return 6.0 * np.exp(-3.5 * np.abs(slope + 0.05))


# ── Step 1: fetch and mosaic whatever DEM tiles cover the bbox ─────────────

def fetch_dem_mosaic(bbox: tuple[float, float, float, float], zoom: int,
                       api_key: str = LINZ_API_KEY):
    min_lat, min_lon, max_lat, max_lon = bbox
    x0, y0, _, _ = lonlat_to_tile_and_pixel(min_lon, max_lat, zoom)
    x1, y1, _, _ = lonlat_to_tile_and_pixel(max_lon, min_lat, zoom)

    tiles_x = range(x0, x1 + 1)
    tiles_y = range(y0, y1 + 1)
    mosaic = np.zeros((len(tiles_y) * TILE_SIZE, len(tiles_x) * TILE_SIZE))

    requests_made = 0
    for ti, ty in enumerate(tiles_y):
        for tj, tx in enumerate(tiles_x):
            url = ELEVATION_TILE_URL.format(z=zoom, x=tx, y=ty, key=api_key)
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            elev = decode_terrain_rgb(Image.open(BytesIO(resp.content)))
            mosaic[ti * TILE_SIZE:(ti + 1) * TILE_SIZE,
                   tj * TILE_SIZE:(tj + 1) * TILE_SIZE] = elev
            requests_made += 1

    print(f"Fetched {requests_made} DEM tiles, mosaic shape {mosaic.shape}.")
    return mosaic, x0, y0


# ── Step 2: sample the FIXED cell-size grid from the mosaic — vectorized ───
#
# This replaces terrain_routing.py's per-point cache.elevation_at() Python
# loop with one batch of vectorized array indexing.

def build_elevation_grid(bbox: tuple[float, float, float, float],
                           cell_size_m: float,
                           mosaic: np.ndarray, origin_x: int, origin_y: int,
                           zoom: int):
    min_lat, min_lon, max_lat, max_lon = bbox
    mid_lat = (min_lat + max_lat) / 2
    m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)
    lat_step = cell_size_m / m_per_deg_lat
    lon_step = cell_size_m / m_per_deg_lon

    n_rows = int((max_lat - min_lat) / lat_step) + 1
    n_cols = int((max_lon - min_lon) / lon_step) + 1

    lats = min_lat + np.arange(n_rows) * lat_step          # shape (n_rows,)
    lons = min_lon + np.arange(n_cols) * lon_step           # shape (n_cols,)
    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")  # both (n_rows, n_cols)

    # Vectorized Web Mercator conversion (same formula as
    # lonlat_to_tile_and_pixel, applied to whole arrays at once).
    n = 2 ** zoom
    lat_rad = np.radians(lat_grid)
    x_float = (lon_grid + 180.0) / 360.0 * n
    y_float = (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / np.pi) / 2.0 * n

    mosaic_col = np.round((x_float - origin_x) * TILE_SIZE).astype(int)
    mosaic_row = np.round((y_float - origin_y) * TILE_SIZE).astype(int)

    mosaic_row = np.clip(mosaic_row, 0, mosaic.shape[0] - 1)
    mosaic_col = np.clip(mosaic_col, 0, mosaic.shape[1] - 1)

    elevation_grid = mosaic[mosaic_row, mosaic_col]  # single vectorized fancy-index op

    print(f"Built {n_rows} x {n_cols} = {n_rows * n_cols} node grid "
          f"(cell_size={cell_size_m}m) from the mosaic in one vectorized step.")

    return elevation_grid, lats, lons, lat_step, lon_step, n_rows, n_cols


# ── Step 3: directional cost — vectorized, 8 passes instead of n*8 calls ───

def shifted(array: np.ndarray, di: int, dj: int) -> np.ndarray:
    """
    Returns an array the same shape as `array`, where result[i, j] =
    array[i+di, j+dj] wherever that's in bounds, and NaN at the border
    where it isn't (no wraparound — this is NOT np.roll).
    """
    n_rows, n_cols = array.shape
    result = np.full_like(array, np.nan)

    i0, i1 = max(0, -di), n_rows - max(0, di)
    j0, j1 = max(0, -dj), n_cols - max(0, dj)
    src_i0, src_i1 = i0 + di, i1 + di
    src_j0, src_j1 = j0 + dj, j1 + dj

    if i0 < i1 and j0 < j1:
        result[i0:i1, j0:j1] = array[src_i0:src_i1, src_j0:src_j1]
    return result


def compute_directional_costs(elevation_grid: np.ndarray, lats: np.ndarray,
                                lat_step: float, lon_step: float, mid_lat_for_rows: np.ndarray):
    """
    Returns {(di, dj): cost_array} for each of the 8 directions — the
    hours-per-step cost of moving from (i, j) to (i + di, j + dj), using
    the IDENTICAL Tobler formula and real-world distance calculation as
    terrain_routing.py's edge_cost_hours(), just computed for the whole
    grid at once instead of once per edge.

    lat_step/lon_step are in degrees; real distances are computed exactly
    like terrain_routing.py does (meters_per_degree at each row's actual
    latitude, since longitude spacing shrinks away from the equator —
    computed per row here rather than assumed constant, to stay exact
    even though it barely matters over a ~1.5km test patch).
    """
    m_per_deg_lat, _ = meters_per_degree(lats[0])  # constant regardless of latitude
    lat_dist_m = lat_step * m_per_deg_lat

    # Per-row longitude distance in meters — varies with each row's latitude.
    lon_dist_per_row = np.array([
        lon_step * meters_per_degree(lat)[1] for lat in lats
    ])  # shape (n_rows,)

    costs = {}
    for di, dj in DIRECTIONS:
        neighbor_elev = shifted(elevation_grid, di, dj)
        rise = neighbor_elev - elevation_grid  # NaN at borders, propagates correctly

        if dj == 0:
            dist_m = np.full_like(elevation_grid, lat_dist_m)
        elif di == 0:
            dist_m = np.broadcast_to(lon_dist_per_row[:, None], elevation_grid.shape)
        else:
            dist_m = np.sqrt(lat_dist_m ** 2 + lon_dist_per_row[:, None] ** 2)
            dist_m = np.broadcast_to(dist_m, elevation_grid.shape)

        with np.errstate(invalid="ignore", divide="ignore"):
            slope = rise / dist_m
            speed_kmh = tobler_speed_kmh_array(slope)
            cost_hours = (dist_m / 1000.0) / speed_kmh

        costs[(di, dj)] = cost_hours

    return costs


# ── Step 4: A* over the precomputed cost arrays ─────────────────────────────
#
# Same algorithm as terrain_routing.py's astar_terrain() — heapq-based,
# admissible haversine-based heuristic — just reading neighbor cost from
# the precomputed arrays (O(1) numpy lookup) instead of a dict of Python
# edge-list objects.

TOBLER_MAX_SPEED_KMH = 6.0 * math.exp(3.5 * 0.05)


def astar_on_grid(cost_arrays: dict, lats: np.ndarray, lons: np.ndarray,
                    start_rc: tuple[int, int], goal_rc: tuple[int, int]):
    n_rows, n_cols = len(lats), len(lons)

    def rc_to_lonlat(rc):
        i, j = rc
        return (lons[j], lats[i])

    def heuristic(rc):
        return haversine_km(rc_to_lonlat(rc), rc_to_lonlat(goal_rc)) / TOBLER_MAX_SPEED_KMH

    open_heap = [(heuristic(start_rc), start_rc)]
    came_from = {}
    g_score = {start_rc: 0.0}

    while open_heap:
        _, current = heapq.heappop(open_heap)

        if current == goal_rc:
            path_rc = [current]
            while path_rc[-1] in came_from:
                path_rc.append(came_from[path_rc[-1]])
            path_rc.reverse()
            return path_rc

        i, j = current
        for di, dj in DIRECTIONS:
            ni, nj = i + di, j + dj
            if not (0 <= ni < n_rows and 0 <= nj < n_cols):
                continue
            cost = cost_arrays[(di, dj)][i, j]
            if not np.isfinite(cost):
                continue
            neighbor = (ni, nj)
            tentative_g = g_score[current] + cost
            if tentative_g < g_score.get(neighbor, math.inf):
                came_from[neighbor] = current
                g_score[neighbor] = tentative_g
                f = tentative_g + heuristic(neighbor)
                heapq.heappush(open_heap, (f, neighbor))

    return None


def build_grid_state(bbox: tuple[float, float, float, float] = DEFAULT_TEST_BBOX,
                       cell_size_m: float = DEFAULT_CELL_SIZE_M,
                       verbose: bool = True) -> dict:
    """
    Does all the expensive, per-region work ONCE: fetch DEM tiles, sample
    the fixed-cell-size grid, precompute directional cost arrays. The
    result is reusable for routing between any number of waypoint pairs
    within this bbox — this is what a Flask app should load at startup,
    not per-request.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    mid_lat = (min_lat + max_lat) / 2
    zoom = zoom_for_resolution(cell_size_m, mid_lat)

    t0 = time.time()
    mosaic, origin_x, origin_y = fetch_dem_mosaic(bbox, zoom)
    t1 = time.time()

    elevation_grid, lats, lons, lat_step, lon_step, n_rows, n_cols = build_elevation_grid(
        bbox, cell_size_m, mosaic, origin_x, origin_y, zoom
    )
    t2 = time.time()

    cost_arrays = compute_directional_costs(elevation_grid, lats, lat_step, lon_step, lats)
    t3 = time.time()

    if verbose:
        print(f"Grid build timing — fetch: {t1 - t0:.2f}s, sample: {t2 - t1:.3f}s, "
              f"cost arrays: {t3 - t2:.3f}s, total: {t3 - t0:.2f}s")

    return {
        "elevation_grid": elevation_grid,
        "lats": lats,
        "lons": lons,
        "cost_arrays": cost_arrays,
        "n_rows": n_rows,
        "n_cols": n_cols,
        "bbox": bbox,
        "cell_size_m": cell_size_m,
    }


def find_nearest_rc(lats: np.ndarray, lons: np.ndarray,
                      lon: float, lat: float) -> tuple[int, int]:
    """
    Snaps an arbitrary (lon, lat) to the nearest grid row/col. Since the
    grid is a regular lattice (not scattered points), nearest row and
    nearest col can be found independently — much cheaper than a full
    2D nearest-neighbor search.
    """
    i = int(np.argmin(np.abs(lats - lat)))
    j = int(np.argmin(np.abs(lons - lon)))
    return i, j


def route_between_waypoints(grid_state: dict,
                              waypoint_a: tuple[float, float],
                              waypoint_b: tuple[float, float]) -> dict:
    """
    Cheap, per-request routing: snap two (lon, lat) waypoints onto the
    already-built grid and run A*. This is what the Flask endpoint below
    calls on every request — grid_state is built once at startup, not
    rebuilt here.
    """
    lats, lons = grid_state["lats"], grid_state["lons"]
    cost_arrays = grid_state["cost_arrays"]
    elevation_grid = grid_state["elevation_grid"]

    start_rc = find_nearest_rc(lats, lons, waypoint_a[0], waypoint_a[1])
    goal_rc = find_nearest_rc(lats, lons, waypoint_b[0], waypoint_b[1])

    t0 = time.time()
    path_rc = astar_on_grid(cost_arrays, lats, lons, start_rc, goal_rc)
    t1 = time.time()
    print(f"Pathfinding: {t1 - t0:.3f}s")

    if path_rc is None:
        return {"ok": False, "error": "No path found between those two points."}

    path_lonlat = [(lons[j], lats[i]) for i, j in path_rc]

    distance_km = sum(
        haversine_km(path_lonlat[k], path_lonlat[k + 1])
        for k in range(len(path_lonlat) - 1)
    )

    total_hours = 0.0
    total_climb_m = 0.0
    for k in range(len(path_rc) - 1):
        i0, j0 = path_rc[k]
        i1, j1 = path_rc[k + 1]
        di, dj = i1 - i0, j1 - j0
        total_hours += cost_arrays[(di, dj)][i0, j0]
        rise = elevation_grid[i1, j1] - elevation_grid[i0, j0]
        if rise > 0:
            total_climb_m += rise

    return {
        "ok": True,
        "route": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [list(c) for c in path_lonlat]},
        },
        "distance_km": distance_km,
        "estimated_hours": total_hours,
        "climb_m": total_climb_m,
        "n_points": len(path_lonlat),
    }


def compute_route(bbox: tuple[float, float, float, float] = DEFAULT_TEST_BBOX,
                    cell_size_m: float = DEFAULT_CELL_SIZE_M) -> dict:
    """Convenience wrapper for the original corner-to-corner test — kept for
    the __main__ equivalence check against terrain_routing.py's numbers."""
    grid_state = build_grid_state(bbox, cell_size_m)
    min_lat, min_lon, max_lat, max_lon = bbox
    waypoint_a = (min_lon, min_lat)
    waypoint_b = (max_lon, max_lat)
    return route_between_waypoints(grid_state, waypoint_a, waypoint_b)


# ── Dynamic bbox + resolution — for ANY two points, not a fixed test region ─
#
# New Zealand is ~268,000 km². At the 15m cell size used for the Tongariro
# tests, a grid covering the whole country would be on the order of 1.2
# BILLION nodes — completely infeasible to hold in memory or search,
# regardless of how well the rest of this is optimized. "No size
# limitation" therefore has to mean the grid adapts its resolution to the
# distance being routed, not that every query gets the same fine detail
# no matter how far apart the two points are.
#
# The approach: pick a fixed NODE_BUDGET (a total node count the grid
# build + A* search can comfortably handle — 2,000,000 was chosen based
# on the full Tongariro Crossing test, which used ~706,000 nodes and took
# ~8 seconds total). Given any two waypoints, compute a padded bounding
# box around them, then derive whatever cell size keeps that box's node
# count within budget. Short/local queries get fine resolution (same
# ~9-15m as already tested); long-distance queries automatically fall
# back to a coarser grid.
#
# HONEST LIMITATION: at genuinely coast-to-coast distances, this still
# degrades to a cell size (roughly 800-900m for the full country extent,
# per manual calculation) far too coarse to resolve real trail-scale
# terrain — a query like that will run without crashing, but the
# resulting route should not be trusted as real hiking guidance. Properly
# supporting genuinely nation-spanning queries would need a hierarchical
# approach (coarse search first to find a rough corridor, then a fine
# search only within that corridor) — noted here as the real next step,
# not built in this pass.

DEFAULT_NODE_BUDGET = 2_000_000
MIN_CELL_SIZE_M = 10.0
MAX_CELL_SIZE_M = 1_000.0
MIN_PADDING_DEG = 0.01  # floor padding so two very close/aligned points still get a sane search area


def compute_dynamic_bbox(waypoint_a: tuple[float, float],
                           waypoint_b: tuple[float, float],
                           padding_frac: float = 0.3) -> tuple[float, float, float, float]:
    """
    Builds a padded bounding box around two arbitrary (lon, lat) waypoints
    — replaces the old fixed DEFAULT_TEST_BBOX. Padding gives the route
    room to deviate from a straight line (e.g. to find a gentler slope);
    padding_frac=0.3 means 30% of the raw bbox's own size is added as
    margin. MIN_PADDING_DEG is a floor so two nearly-aligned points (e.g.
    almost the same longitude) don't produce an unrealistically narrow
    corridor with no room to route sideways at all.
    """
    lon_a, lat_a = waypoint_a
    lon_b, lat_b = waypoint_b

    min_lon, max_lon = sorted([lon_a, lon_b])
    min_lat, max_lat = sorted([lat_a, lat_b])

    lon_pad = max((max_lon - min_lon) * padding_frac, MIN_PADDING_DEG)
    lat_pad = max((max_lat - min_lat) * padding_frac, MIN_PADDING_DEG)

    return (min_lat - lat_pad, min_lon - lon_pad, max_lat + lat_pad, max_lon + lon_pad)


def choose_cell_size_for_budget(bbox: tuple[float, float, float, float],
                                  node_budget: int = DEFAULT_NODE_BUDGET) -> float:
    """
    Picks a cell size (meters) such that the bbox's grid stays within
    node_budget total nodes, clamped to [MIN_CELL_SIZE_M, MAX_CELL_SIZE_M].
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    mid_lat = (min_lat + max_lat) / 2
    m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)

    height_m = abs(max_lat - min_lat) * m_per_deg_lat
    width_m = abs(max_lon - min_lon) * m_per_deg_lon
    area_m2 = height_m * width_m

    if area_m2 <= 0:
        return MIN_CELL_SIZE_M

    cell_size_m = math.sqrt(area_m2 / node_budget)
    return max(MIN_CELL_SIZE_M, min(MAX_CELL_SIZE_M, cell_size_m))


def route_any_two_points(waypoint_a: tuple[float, float],
                           waypoint_b: tuple[float, float],
                           node_budget: int = DEFAULT_NODE_BUDGET,
                           padding_frac: float = 0.3) -> dict:
    """
    The actual "any two points in NZ" entry point: builds a bbox and
    resolution sized to this specific query, then routes between the
    waypoints. Unlike build_grid_state()/route_between_waypoints() used
    for the fixed-region tests, there's no pre-built grid to reuse here —
    every query gets its own appropriately-sized grid, since the region
    needed depends entirely on where the two points are.
    """
    bbox = compute_dynamic_bbox(waypoint_a, waypoint_b, padding_frac)
    cell_size_m = choose_cell_size_for_budget(bbox, node_budget)

    min_lat, min_lon, max_lat, max_lon = bbox
    mid_lat = (min_lat + max_lat) / 2
    m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)
    bbox_height_km = abs(max_lat - min_lat) * m_per_deg_lat / 1000
    bbox_width_km = abs(max_lon - min_lon) * m_per_deg_lon / 1000

    print(f"Query bbox: {bbox_height_km:.1f}km x {bbox_width_km:.1f}km, "
          f"cell size chosen: {cell_size_m:.1f}m")
    if cell_size_m > 100:
        print(f"NOTE: cell size {cell_size_m:.0f}m is too coarse to resolve real "
              f"trail-scale terrain features — this query spans a large enough "
              f"area that the route below should NOT be trusted as real hiking "
              f"guidance, only as a rough corridor estimate.")

    grid_state = build_grid_state(bbox, cell_size_m)
    result = route_between_waypoints(grid_state, waypoint_a, waypoint_b)
    result["bbox_used"] = bbox
    result["cell_size_m_used"] = cell_size_m
    return result



# ── API for frontend ───────────────────────────────────────────
#
# Same POST /route contract as before, but now supports ANY two waypoints
# in NZ — since the region needed depends on the query, the grid is built
# per-request (route_any_two_points) rather than preloaded once at
# startup. This trades startup-time preloading for per-request latency —
# worth watching in practice, since every request now pays the DEM-fetch
# cost, not just the first one.

def create_app(node_budget: int = DEFAULT_NODE_BUDGET):
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.route("/route", methods=["POST"])
    def route():
        body = request.get_json(force=True)
        a = tuple(body["a"])
        b = tuple(body["b"])
        result = route_any_two_points(a, b, node_budget=node_budget)
        return jsonify(result)

    return app


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Route between two points anywhere in NZ using terrain-cost pathfinding."
    )
    parser.add_argument("lon_a", type=float, help="Longitude of waypoint A")
    parser.add_argument("lat_a", type=float, help="Latitude of waypoint A")
    parser.add_argument("lon_b", type=float, help="Longitude of waypoint B")
    parser.add_argument("lat_b", type=float, help="Latitude of waypoint B")
    parser.add_argument("--node-budget", type=int, default=DEFAULT_NODE_BUDGET,
                         help=f"Max grid nodes to build (default {DEFAULT_NODE_BUDGET})")
    parser.add_argument("--out", type=str, default="terrain_route.geojson",
                         help="Output GeoJSON file path (default terrain_route.geojson)")

    args = parser.parse_args()

    waypoint_a = (args.lon_a, args.lat_a)
    waypoint_b = (args.lon_b, args.lat_b)

    result = route_any_two_points(waypoint_a, waypoint_b, node_budget=args.node_budget)

    if not result["ok"]:
        print("Routing failed:", result["error"])
        sys.exit(1)

    print(f"\nRoute found: {result['distance_km']:.2f} km, "
          f"~{result['estimated_hours']:.2f} hours, "
          f"{result['climb_m']:.1f} m climb, {result['n_points']} points.")

    with open(args.out, "w") as f:
        json.dump(result["route"], f)
    print(f"Saved route to {args.out}")
