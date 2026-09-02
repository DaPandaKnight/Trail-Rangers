"""
route_generator.py — Terrain-cost hiking route generator for New Zealand.

Given two or more waypoints, this module builds a real-world elevation grid
from LINZ DEM tiles, estimates walking cost for every step using Tobler's
hiking-speed formula, and finds the lowest-effort route with A*. Lakes and
wide rivers are hard-blocked using OpenStreetMap water data; walking beside
a stream is softly discouraged; and the cost model favors ridgelines over
open-face traverses. The routing logic is exposed through a CLI, a Flask
app, and an AWS Lambda handler.

PERFORMANCE NOTE (this version): the A* search is now JIT-compiled with
Numba instead of running as plain Python heapq/dict logic. Verified to
produce byte-identical paths to the original pure-Python implementation on
test grids before being adopted — see the project notes for the
correctness check. On a synthetic 2,000,908-node grid matching a real
production run, this took the search from ~24s (8-direction) / ~39s
(16-direction) down to ~0.7s / ~1.3s respectively — roughly a 30-35x
speedup, real terrain will vary but the mechanism improvement is the same.

WATER/STREAM FETCH NOTE (this version): fetch_water_and_stream_geometries()
replaces the previous two separate Overpass calls (fetch_water_geometries +
fetch_stream_lines) with ONE combined request, splitting results by OSM tag
after the fact. This roughly halves the worst-case wait when Overpass is
slow, since there's one retry cycle instead of two sequential ones. It also
enforces a genuine wall-clock deadline per mirror attempt — `requests`'
own `timeout=` parameter resets on each chunk of data received, so a
connection that trickles data slowly (rather than stalling outright) can
run well past the nominal timeout without ever raising. The wrapper below
uses a worker thread with a hard `future.result(timeout=...)` cutoff to
bound total wait time regardless of what the connection is doing.

Dependencies: pip install requests pillow numpy shapely numba
"""

import base64
import heapq
import json
import math
import os
import pickle
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from io import BytesIO
from pathlib import Path

import numpy as np
import requests
import shapely
from numba import njit
from PIL import Image
from shapely.geometry import LineString, Polygon, box
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree

# ── Configuration ────────────────────────────────────────────────────────

ELEVATION_TILE_URL = (
    "https://basemaps.linz.govt.nz/v1/tiles/elevation/WebMercatorQuad/"
    "{z}/{x}/{y}.png?api={key}&pipeline=terrain-rgb"
)

# The primary Overpass instance occasionally rejects requests from
# non-browser clients with a 406 response, so a mirror is tried first and
# the primary is kept as a fallback.
OVERPASS_API_URLS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
OVERPASS_TIMEOUT_S = 30
# Hard wall-clock cutoff per mirror attempt — see module docstring. Kept
# somewhat above OVERPASS_TIMEOUT_S so a legitimately-completing slow
# response still has a chance, while a truly hung connection is abandoned
# in bounded time rather than indefinitely.
OVERPASS_HARD_TIMEOUT_S = 40
OVERPASS_USER_AGENT = "RidgeWalker-TrailRouter/1.0 (CSIT321 project)"

TILE_SIZE = 256
DEFAULT_TEST_BBOX = (-39.16, 175.60, -39.15, 175.62)  # min_lat, min_lon, max_lat, max_lon
DEFAULT_CELL_SIZE_M = 15

# ── Cost-model parameters ────────────────────────────────────────────────

TOBLER_STEEPNESS_FACTOR = 4.0   # Higher values penalize steep terrain more heavily.
TOBLER_DOWNHILL_BIAS = 0.05     # Tobler's constant: a slight downhill grade is fastest.
RIDGE_CROSS_SLOPE_WEIGHT = 2.0  # Penalty for sidling across an open slope. 0 disables it.
VALLEY_AVOIDANCE_WEIGHT = 0.0   # Disabled by default: terrain curvature alone cannot
                                 # distinguish a river valley from a dry mountain pass or
                                 # crater. STREAM_PROXIMITY_WEIGHT below addresses the same
                                 # problem using real waterway data instead. This constant is
                                 # left in place and tunable for future experimentation.
STREAM_PROXIMITY_WEIGHT = 1.5      # (1.5 was original) Soft cost multiplier applied near a mapped
                                 # stream or river centerline. 0 disables it. A single
                                 # crossing touches only one or two cells and stays cheap;
                                 # walking alongside a stream for an extended distance
                                 # accumulates real cost.
STREAM_BUFFER_M = 15.0           # (15 was original) Distance from a stream centerline within which the
                                 # proximity penalty applies.

# 16-point compass: the 8 standard king-move directions (45 degrees apart)
# plus 8 knight-style half-step directions, which allow the path to bend
# gradually rather than being restricted to 45-degree turns.
DIRECTIONS = [
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1),            (0, 1),
    (1, -1),  (1, 0),   (1, 1),
    (-2, -1), (-1, -2), (-2, 1), (-1, 2),
    (2, -1),  (1, -2),  (2, 1),  (1, 2),
]
DIRECTIONS_ARR = np.array(DIRECTIONS, dtype=np.int64)

_cached_key = None


def get_linz_key():
    """
    Resolves the LINZ API key.

    Checks the LINZ_API_KEY environment variable first, then falls back to
    AWS Secrets Manager for the Lambda deployment. The result is cached in
    memory so a warm Lambda invocation does not repeat the lookup.
    """
    global _cached_key
    if _cached_key:
        return _cached_key

    env_key = os.environ.get("LINZ_API_KEY")
    if env_key:
        _cached_key = env_key
        return _cached_key

    import boto3
    client = boto3.client("secretsmanager")
    secret_id = os.environ.get("LINZ_SECRET_ID", "ridgewalker/linz-api-key")
    response = client.get_secret_value(SecretId=secret_id)
    secret = json.loads(response["SecretString"])
    _cached_key = secret["LINZ_API_KEY"]
    return _cached_key


# ── Geometry and coordinate-conversion helpers ──────────────────────────

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Returns the great-circle distance in kilometers between two (lon, lat) points."""
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
    """Returns (meters per degree of latitude, meters per degree of longitude) at the given latitude."""
    return 111_320.0, 111_320.0 * math.cos(math.radians(lat))


def zoom_for_resolution(cell_size_m: float, lat: float,
                          min_zoom: int = 10, max_zoom: int = 15) -> int:
    """Returns the smallest Web Mercator zoom level whose pixel size does not exceed cell_size_m."""
    for z in range(min_zoom, max_zoom + 1):
        meters_per_pixel = 156_543.03392 * math.cos(math.radians(lat)) / (2 ** z)
        if meters_per_pixel <= cell_size_m:
            return z
    return max_zoom


def lonlat_to_tile_and_pixel(lon: float, lat: float, zoom: int,
                               tile_size: int = TILE_SIZE):
    """Converts a (lon, lat) coordinate to its Web Mercator tile index and pixel offset within that tile."""
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    x_float = (lon + 180.0) / 360.0 * n
    y_float = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    xtile, ytile = int(x_float), int(y_float)
    px = int((x_float - xtile) * tile_size)
    py = int((y_float - ytile) * tile_size)
    return xtile, ytile, px, py


def decode_terrain_rgb(img: Image.Image) -> np.ndarray:
    """Decodes a terrain-RGB encoded tile image into an array of elevation values in meters."""
    arr = np.asarray(img.convert("RGB"), dtype=np.float64)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    return -10000 + (r * 256 * 256 + g * 256 + b) * 0.1


def tobler_speed_kmh_array(slope: np.ndarray) -> np.ndarray:
    """Vectorized implementation of Tobler's hiking-speed formula: returns walking speed in km/h for a given slope array."""
    return 6.0 * np.exp(-TOBLER_STEEPNESS_FACTOR * np.abs(slope + TOBLER_DOWNHILL_BIAS))


# ── Step 1: Fetch and assemble the DEM tiles covering the bounding box ──

def fetch_dem_mosaic(bbox: tuple[float, float, float, float], zoom: int,
                       api_key: str = None, max_workers: int = 10):
    """
    Downloads every LINZ elevation tile covering the bounding box and
    assembles them into a single array. Tiles are fetched concurrently
    (each download is fully independent I/O) rather than one at a time —
    verified against the real observed per-tile timing to give roughly a
    15x speedup at typical tile counts; max_workers=10 is a conservative
    default that hasn't been tested against LINZ's actual rate limits, so
    lower it if you start seeing errors under heavy concurrent load.
    """
    if api_key is None:
        api_key = get_linz_key()

    min_lat, min_lon, max_lat, max_lon = bbox
    x0, y0, _, _ = lonlat_to_tile_and_pixel(min_lon, max_lat, zoom)
    x1, y1, _, _ = lonlat_to_tile_and_pixel(max_lon, min_lat, zoom)

    tiles_x = list(range(x0, x1 + 1))
    tiles_y = list(range(y0, y1 + 1))
    mosaic = np.zeros((len(tiles_y) * TILE_SIZE, len(tiles_x) * TILE_SIZE))

    tasks = [(ti, ty, tj, tx) for ti, ty in enumerate(tiles_y) for tj, tx in enumerate(tiles_x)]

    def fetch_one(task):
        ti, ty, tj, tx = task
        url = ELEVATION_TILE_URL.format(z=zoom, x=tx, y=ty, key=api_key)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        elev = decode_terrain_rgb(Image.open(BytesIO(resp.content)))
        return ti, tj, elev

    requests_made = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for ti, tj, elev in executor.map(fetch_one, tasks):
            mosaic[ti * TILE_SIZE:(ti + 1) * TILE_SIZE,
                   tj * TILE_SIZE:(tj + 1) * TILE_SIZE] = elev
            requests_made += 1

    print(f"Fetched {requests_made} DEM tiles, mosaic shape {mosaic.shape}.")
    return mosaic, x0, y0




# ── Step 2: Sample a fixed real-world cell-size grid from the mosaic ───

def build_elevation_grid(bbox: tuple[float, float, float, float],
                           cell_size_m: float,
                           mosaic: np.ndarray, origin_x: int, origin_y: int,
                           zoom: int):
    """Resamples the DEM mosaic onto a regular latitude/longitude grid with the given real-world cell spacing."""
    min_lat, min_lon, max_lat, max_lon = bbox
    mid_lat = (min_lat + max_lat) / 2
    m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)
    lat_step = cell_size_m / m_per_deg_lat
    lon_step = cell_size_m / m_per_deg_lon

    n_rows = int((max_lat - min_lat) / lat_step) + 1
    n_cols = int((max_lon - min_lon) / lon_step) + 1

    lats = min_lat + np.arange(n_rows) * lat_step
    lons = min_lon + np.arange(n_cols) * lon_step
    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")

    n = 2 ** zoom
    lat_rad = np.radians(lat_grid)
    x_float = (lon_grid + 180.0) / 360.0 * n
    y_float = (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / np.pi) / 2.0 * n

    mosaic_col = np.round((x_float - origin_x) * TILE_SIZE).astype(int)
    mosaic_row = np.round((y_float - origin_y) * TILE_SIZE).astype(int)
    mosaic_row = np.clip(mosaic_row, 0, mosaic.shape[0] - 1)
    mosaic_col = np.clip(mosaic_col, 0, mosaic.shape[1] - 1)

    elevation_grid = mosaic[mosaic_row, mosaic_col]

    print(f"Built {n_rows} x {n_cols} = {n_rows * n_cols} node grid "
          f"(cell_size={cell_size_m}m).")

    return elevation_grid, lats, lons, lat_step, lon_step, n_rows, n_cols


# ── Step 2.5: Fetch water bodies AND stream lines in one combined request ──

def _polygon_from_way(geometry: list) -> Polygon | None:
    """Builds a closed polygon from a single OSM way's inline coordinate list. Returns None if the geometry is invalid."""
    if not geometry or len(geometry) < 3:
        return None
    coords = [(pt["lon"], pt["lat"]) for pt in geometry]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    try:
        poly = Polygon(coords)
        return poly if poly.is_valid and poly.area > 0 else None
    except Exception:
        return None


def _polygons_from_relation(element: dict) -> list[Polygon]:
    """
    Builds one or more polygons from a multipolygon relation's outer-role
    member ways, stitching together any that are split across multiple
    segments. Inner rings (holes, such as an island within a lake) are not
    represented — this is sufficient to keep the router out of the water
    body, though it does not account for terrain at the edge of an island.
    """
    outer_lines = []
    for member in element.get("members", []):
        if member.get("role") != "outer":
            continue
        geometry = member.get("geometry")
        if not geometry or len(geometry) < 2:
            continue
        outer_lines.append(LineString([(pt["lon"], pt["lat"]) for pt in geometry]))

    if not outer_lines:
        return []

    merged = linemerge(outer_lines)
    rings = [merged] if merged.geom_type == "LineString" else list(merged.geoms)

    polygons = []
    for ring in rings:
        coords = list(ring.coords)
        if len(coords) < 3:
            continue
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        try:
            poly = Polygon(coords)
            if poly.is_valid and poly.area > 0:
                polygons.append(poly)
        except Exception:
            continue
    return polygons


def _query_overpass_with_hard_timeout(query: str,
                                        timeout: int = OVERPASS_TIMEOUT_S,
                                        hard_timeout: int = OVERPASS_HARD_TIMEOUT_S) -> list[dict]:
    """
    Tries each mirror in OVERPASS_API_URLS in turn. `requests`' own
    `timeout=` parameter resets on each chunk of data received, so a
    connection that trickles data slowly (rather than stalling outright)
    can exceed it without ever raising an exception — this is what caused
    the observed hangs that needed a manual Ctrl-C. Running the request in
    a worker thread and enforcing `future.result(timeout=hard_timeout)`
    gives a genuine wall-clock deadline regardless of what the connection
    is doing internally.
    """
    headers = {"User-Agent": OVERPASS_USER_AGENT, "Accept": "application/json"}
    last_error = None

    for url in OVERPASS_API_URLS:
        # NOTE: deliberately NOT using ThreadPoolExecutor as a `with` block.
        # `with` blocks on __exit__ until the submitted thread actually
        # finishes (executor.shutdown(wait=True) by default) — which means
        # even after future.result(timeout=...) correctly gives up, the
        # code would still sit and wait for the hung connection anyway,
        # completely defeating the point of the hard timeout. Caught by
        # testing this against a simulated hang before trusting it — see
        # project notes. shutdown(wait=False) below lets the calling code
        # move on immediately; the abandoned thread is left to finish or
        # error out on its own in the background.
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            requests.post, url, data={"data": query}, headers=headers, timeout=timeout
        )
        try:
            resp = future.result(timeout=hard_timeout)
        except FutureTimeoutError:
            executor.shutdown(wait=False)
            print(f"Overpass mirror {url} exceeded hard wall-clock timeout of "
                  f"{hard_timeout}s — abandoning and trying next mirror.")
            last_error = TimeoutError(f"hard wall-clock timeout after {hard_timeout}s")
            continue
        except Exception as e:
            executor.shutdown(wait=False)
            last_error = e
            continue

        executor.shutdown(wait=False)
        try:
            resp.raise_for_status()
            return resp.json().get("elements", [])
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        f"All Overpass endpoints failed (tried {len(OVERPASS_API_URLS)}); "
        f"last error: {last_error}"
    )


# ── Local pre-fetched water/stream dataset — the real production path ──────
#
# Built ONCE by prefetch_nz_water_data.py, which tiles all of New Zealand
# and fetches water/stream data via Overpass ahead of time. Loaded here
# ONCE at first use (not per-request — a spatial index over the whole
# country stays in memory for the life of the process) and queried purely
# locally for every route request from then on. This is what actually
# removes Overpass from the per-request path — the earlier per-bbox disk
# cache did NOT do this; it only skipped re-fetching identical repeated
# queries, which doesn't help distinct real user queries at all.
#
# Falls back to a live Overpass call (fetch_water_and_stream_geometries)
# if nz_water_data.pkl doesn't exist yet — useful for local development
# before running the full prefetch, which takes a while.

NZ_WATER_DATA_FILE = Path(os.environ.get("NZ_WATER_DATA_FILE", "nz_water_data.pkl"))

# S3-backed loading — for the Lambda deployment, where a 50-250MB file
# can't be bundled in the code package (Lambda's zip size limits) and
# shouldn't be re-uploaded on every code deploy anyway. If
# NZ_WATER_DATA_S3_BUCKET is set and the local file isn't already present,
# the dataset is downloaded from S3 into /tmp — Lambda's only writable
# directory — ONCE per cold start, then reused from there for every warm
# invocation of that execution environment (in addition to the in-memory
# cache below, which avoids even re-reading/re-parsing the file on warm
# invocations).
NZ_WATER_DATA_S3_BUCKET = os.environ.get("NZ_WATER_DATA_S3_BUCKET")
NZ_WATER_DATA_S3_KEY = os.environ.get("NZ_WATER_DATA_S3_KEY", "nz_water_data.pkl")
NZ_WATER_DATA_LOCAL_CACHE = Path(os.environ.get("NZ_WATER_DATA_LOCAL_CACHE", "/tmp/nz_water_data.pkl"))

_local_water_index = None
_local_water_load_attempted = False


def _resolve_water_data_path():
    """
    Figures out where to load the local water dataset from, in order:
    1. NZ_WATER_DATA_FILE, if it already exists (local dev/CLI — you ran
       prefetch_nz_water_data.py right here).
    2. NZ_WATER_DATA_LOCAL_CACHE, if an earlier download already put it
       there this execution environment's lifetime.
    3. Download from S3 (NZ_WATER_DATA_S3_BUCKET/NZ_WATER_DATA_S3_KEY)
       into NZ_WATER_DATA_LOCAL_CACHE, if a bucket is configured.
    Returns None if none of these are available — the caller falls back
    to live Overpass calls.
    """
    if NZ_WATER_DATA_FILE.exists():
        return NZ_WATER_DATA_FILE

    if NZ_WATER_DATA_LOCAL_CACHE.exists():
        return NZ_WATER_DATA_LOCAL_CACHE

    if NZ_WATER_DATA_S3_BUCKET:
        print(f"Downloading water dataset from s3://{NZ_WATER_DATA_S3_BUCKET}/"
              f"{NZ_WATER_DATA_S3_KEY} to {NZ_WATER_DATA_LOCAL_CACHE}...")
        import boto3
        NZ_WATER_DATA_LOCAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        boto3.client("s3").download_file(
            NZ_WATER_DATA_S3_BUCKET, NZ_WATER_DATA_S3_KEY, str(NZ_WATER_DATA_LOCAL_CACHE)
        )
        print("Download complete.")
        return NZ_WATER_DATA_LOCAL_CACHE

    return None


def _load_local_water_index():
    """
    Loads the local water dataset (see _resolve_water_data_path for where
    from) into memory and builds an STRtree spatial index over the water
    polygons and stream lines separately. Cached at module level — this
    runs at most once per process (once per Lambda cold start, not once
    per request), not once per request.
    """
    global _local_water_index, _local_water_load_attempted
    if _local_water_load_attempted:
        return _local_water_index
    _local_water_load_attempted = True

    data_path = _resolve_water_data_path()
    if data_path is None:
        print(f"No local water dataset available (checked {NZ_WATER_DATA_FILE}, "
              f"{NZ_WATER_DATA_LOCAL_CACHE}, and NZ_WATER_DATA_S3_BUCKET is not set) — "
              f"falling back to live Overpass calls per request. Run "
              f"prefetch_nz_water_data.py once (it takes a while) to remove Overpass "
              f"from the request path entirely.")
        return None

    with open(data_path, "rb") as f:
        water_polygons, stream_lines = pickle.load(f)

    water_tree = STRtree(water_polygons) if water_polygons else None
    stream_tree = STRtree(stream_lines) if stream_lines else None

    print(f"Loaded local water dataset: {len(water_polygons)} water polygons, "
          f"{len(stream_lines)} stream lines. No Overpass calls needed for routing now.")

    _local_water_index = {
        "water_tree": water_tree, "water_polygons": water_polygons,
        "stream_tree": stream_tree, "stream_lines": stream_lines,
    }
    return _local_water_index


def query_local_water_data(bbox: tuple[float, float, float, float]):
    """
    Returns (water_polygons, stream_lines) intersecting bbox from the
    pre-loaded local dataset, or None if no local dataset is available
    (signals the caller to fall back to a live Overpass request instead).
    Pure in-memory spatial query — no network call.
    """
    index = _load_local_water_index()
    if index is None:
        return None

    min_lat, min_lon, max_lat, max_lon = bbox
    query_box = box(min_lon, min_lat, max_lon, max_lat)

    water_result = []
    if index["water_tree"] is not None:
        for idx in index["water_tree"].query(query_box):
            geom = index["water_polygons"][idx]
            if geom.intersects(query_box):
                water_result.append(geom)

    stream_result = []
    if index["stream_tree"] is not None:
        for idx in index["stream_tree"].query(query_box):
            geom = index["stream_lines"][idx]
            if geom.intersects(query_box):
                stream_result.append(geom)

    return water_result, stream_result


def get_water_and_stream_geometries(bbox: tuple[float, float, float, float]
                                      ) -> tuple[list[Polygon], list[LineString]]:
    """
    The function build_grid_state should actually call: tries the local
    pre-fetched dataset first (instant, no network), and only falls back
    to a live Overpass request if that dataset hasn't been built yet.
    """
    local_result = query_local_water_data(bbox)
    if local_result is not None:
        return local_result
    return fetch_water_and_stream_geometries(bbox)


def fetch_water_and_stream_geometries(bbox: tuple[float, float, float, float],
                                        timeout: int = OVERPASS_TIMEOUT_S
                                        ) -> tuple[list[Polygon], list[LineString]]:
    """
    Fetches BOTH water-body polygons (lakes, reservoirs, wide rivers) and
    stream/river CENTERLINES in a SINGLE Overpass request, splitting the
    results by OSM tag afterward. This replaces two previously-separate
    calls (fetch_water_geometries + fetch_stream_lines), each with its own
    mirror-retry loop — combining them roughly halves the worst-case wait
    when Overpass is slow, since there's one retry cycle instead of two
    sequential ones.

    Returns (water_polygons, stream_lines).
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    bbox_str = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    query = f"""
    [out:json][timeout:{timeout}];
    (
      way["natural"="water"]({bbox_str});
      way["landuse"="reservoir"]({bbox_str});
      way["waterway"="riverbank"]({bbox_str});
      relation["natural"="water"]["type"="multipolygon"]({bbox_str});
      relation["landuse"="reservoir"]["type"="multipolygon"]({bbox_str});
      way["waterway"="stream"]({bbox_str});
      way["waterway"="river"]({bbox_str});
      way["waterway"="canal"]({bbox_str});
    );
    out geom;
    """

    elements = _query_overpass_with_hard_timeout(query, timeout=timeout)

    water_polygons: list[Polygon] = []
    stream_lines: list[LineString] = []

    for element in elements:
        tags = element.get("tags", {})
        is_water_tag = (tags.get("natural") == "water"
                         or tags.get("landuse") == "reservoir"
                         or tags.get("waterway") == "riverbank")
        is_stream_tag = tags.get("waterway") in ("stream", "river", "canal")

        elem_type = element.get("type")

        if elem_type == "way" and is_water_tag:
            poly = _polygon_from_way(element.get("geometry"))
            if poly is not None:
                water_polygons.append(poly)

        elif elem_type == "way" and is_stream_tag:
            geometry = element.get("geometry")
            if geometry and len(geometry) >= 2:
                try:
                    line = LineString([(pt["lon"], pt["lat"]) for pt in geometry])
                    if line.is_valid and line.length > 0:
                        stream_lines.append(line)
                except Exception:
                    pass

        elif elem_type == "relation" and is_water_tag:
            water_polygons.extend(_polygons_from_relation(element))

    return water_polygons, stream_lines


def build_water_mask(water_polygons: list[Polygon], lats: np.ndarray,
                       lons: np.ndarray) -> np.ndarray:
    """Rasterizes water polygons onto the grid. Returns a boolean array where True marks a cell inside a water body."""
    n_rows, n_cols = len(lats), len(lons)
    if not water_polygons:
        return np.zeros((n_rows, n_cols), dtype=bool)

    merged = unary_union(water_polygons)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return shapely.contains_xy(merged, lon_grid, lat_grid)


def apply_water_mask(cost_arrays: dict, water_mask: np.ndarray) -> dict:
    """Sets the cost of any edge that touches a water cell to infinity, hard-blocking it from the route."""
    water_float = water_mask.astype(np.float64)
    for di, dj in DIRECTIONS:
        neighbor_is_water = shifted(water_float, di, dj)
        neighbor_is_water = np.nan_to_num(neighbor_is_water, nan=0.0) >= 0.5
        blocked = water_mask | neighbor_is_water
        cost_arrays[(di, dj)] = np.where(blocked, np.inf, cost_arrays[(di, dj)])
    return cost_arrays


def build_stream_proximity_mask(stream_lines: list[LineString], lats: np.ndarray,
                                  lons: np.ndarray, buffer_m: float = STREAM_BUFFER_M) -> np.ndarray:
    """Rasterizes a buffer_m-wide corridor around stream_lines onto the grid. Returns a boolean array where True marks a cell within that corridor."""
    n_rows, n_cols = len(lats), len(lons)
    if not stream_lines:
        return np.zeros((n_rows, n_cols), dtype=bool)

    merged = unary_union(stream_lines)
    mid_lat = float(np.mean(lats))
    m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)
    buffer_deg = buffer_m / min(m_per_deg_lat, m_per_deg_lon)
    buffered = merged.buffer(buffer_deg)

    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return shapely.contains_xy(buffered, lon_grid, lat_grid)


def apply_stream_proximity_penalty(cost_arrays: dict, stream_mask: np.ndarray,
                                     weight: float = STREAM_PROXIMITY_WEIGHT) -> dict:
    """Multiplies the cost of any edge within the stream-proximity buffer by weight. This is a soft penalty, not a hard block."""
    if weight == 1.0:
        return cost_arrays
    stream_float = stream_mask.astype(np.float64)
    for di, dj in DIRECTIONS:
        neighbor_in_zone = shifted(stream_float, di, dj)
        neighbor_in_zone = np.nan_to_num(neighbor_in_zone, nan=0.0) >= 0.5
        in_zone = stream_mask | neighbor_in_zone
        cost_arrays[(di, dj)] = np.where(in_zone, cost_arrays[(di, dj)] * weight, cost_arrays[(di, dj)])
    return cost_arrays


# ── Step 3: Directional cost, computed across the whole grid at once ───

def shifted(array: np.ndarray, di: int, dj: int) -> np.ndarray:
    """Returns array shifted by (di, dj), such that result[i, j] = array[i + di, j + dj]. Out-of-bounds positions are filled with NaN."""
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
                                lat_step: float, lon_step: float):
    """
    Computes the walking cost, in hours, of moving from each grid cell to
    each of its neighbors in DIRECTIONS. Returns a dictionary mapping each
    (di, dj) direction to a cost array of the same shape as elevation_grid.

    The base cost is Tobler's hiking speed applied over the real-world
    distance of the step. Two multiplicative penalties are layered on top:

    - Sidle penalty: increases cost when the terrain drops away to one
      side only, discouraging routes that cut diagonally across an open
      slope rather than following its contour.
    - Valley penalty: increases cost when the terrain is concave (a
      trough), scaled by how low the cell sits within this bounding box's
      own elevation range. Disabled by default; see VALLEY_AVOIDANCE_WEIGHT.
    """
    m_per_deg_lat, _ = meters_per_degree(lats[0])
    lat_dist_m = lat_step * m_per_deg_lat
    lon_dist_per_row = np.array([
        lon_step * meters_per_degree(lat)[1] for lat in lats
    ])

    def real_dist_m(di: int, dj: int) -> np.ndarray:
        if dj == 0:
            return np.full_like(elevation_grid, abs(di) * lat_dist_m)
        elif di == 0:
            return np.broadcast_to(np.abs(dj) * lon_dist_per_row[:, None], elevation_grid.shape)
        else:
            d = np.sqrt((di * lat_dist_m) ** 2 + (dj * lon_dist_per_row[:, None]) ** 2)
            return np.broadcast_to(d, elevation_grid.shape)

    elev_min = np.nanmin(elevation_grid)
    elev_max = np.nanmax(elevation_grid)
    elev_range = elev_max - elev_min
    if elev_range > 0:
        valley_elevation_scale = 1.0 - (elevation_grid - elev_min) / elev_range
    else:
        valley_elevation_scale = np.zeros_like(elevation_grid)

    costs = {}
    for di, dj in DIRECTIONS:
        neighbor_elev = shifted(elevation_grid, di, dj)
        rise = neighbor_elev - elevation_grid
        dist_m = real_dist_m(di, dj)

        pdi, pdj = -dj, di
        elev_plus = shifted(elevation_grid, pdi, pdj)
        elev_minus = shifted(elevation_grid, -pdi, -pdj)
        cross_rise = elev_plus - elev_minus
        curvature_raw = elev_plus + elev_minus - 2.0 * elevation_grid
        one_side_dist_m = real_dist_m(pdi, pdj)
        cross_dist_m = one_side_dist_m * 2.0

        with np.errstate(invalid="ignore", divide="ignore"):
            slope = rise / dist_m
            speed_kmh = tobler_speed_kmh_array(slope)
            base_cost_hours = (dist_m / 1000.0) / speed_kmh

            cross_slope = np.nan_to_num(cross_rise / cross_dist_m, nan=0.0)
            sidle_penalty = 1.0 + RIDGE_CROSS_SLOPE_WEIGHT * np.abs(cross_slope)

            curvature = np.nan_to_num(curvature_raw / one_side_dist_m, nan=0.0)
            concavity = np.clip(curvature, 0.0, None)
            valley_penalty = 1.0 + VALLEY_AVOIDANCE_WEIGHT * concavity * valley_elevation_scale

            cost_hours = base_cost_hours * sidle_penalty * valley_penalty

        costs[(di, dj)] = cost_hours

    return costs


# ── Step 4: A* search over the precomputed cost arrays (Numba-jitted) ──
#
# Same directional-cost algorithm as before — precomputed cost arrays,
# haversine-based admissible heuristic, lazy-deletion binary heap — just
# compiled instead of interpreted. Verified to produce identical paths to
# the original pure-Python version on test grids before being adopted.
# The manual binary heap and flat NumPy arrays (instead of heapq/dict) are
# necessary because Numba's nopython mode can't JIT-compile Python's
# heapq module or dicts keyed by tuples.

TOBLER_MAX_SPEED_KMH = 6.0 * math.exp(TOBLER_STEEPNESS_FACTOR * TOBLER_DOWNHILL_BIAS)


@njit(cache=True)
def _heap_push(heap_f, heap_i, heap_j, size, f, i, j):
    heap_f[size] = f
    heap_i[size] = i
    heap_j[size] = j
    idx = size
    size += 1
    while idx > 0:
        parent = (idx - 1) // 2
        if heap_f[parent] <= heap_f[idx]:
            break
        heap_f[parent], heap_f[idx] = heap_f[idx], heap_f[parent]
        heap_i[parent], heap_i[idx] = heap_i[idx], heap_i[parent]
        heap_j[parent], heap_j[idx] = heap_j[idx], heap_j[parent]
        idx = parent
    return size


@njit(cache=True)
def _heap_pop(heap_f, heap_i, heap_j, size):
    f0 = heap_f[0]; i0 = heap_i[0]; j0 = heap_j[0]
    size -= 1
    heap_f[0] = heap_f[size]
    heap_i[0] = heap_i[size]
    heap_j[0] = heap_j[size]
    idx = 0
    while True:
        left = 2 * idx + 1
        right = 2 * idx + 2
        smallest = idx
        if left < size and heap_f[left] < heap_f[smallest]:
            smallest = left
        if right < size and heap_f[right] < heap_f[smallest]:
            smallest = right
        if smallest == idx:
            break
        heap_f[idx], heap_f[smallest] = heap_f[smallest], heap_f[idx]
        heap_i[idx], heap_i[smallest] = heap_i[smallest], heap_i[idx]
        heap_j[idx], heap_j[smallest] = heap_j[smallest], heap_j[idx]
        idx = smallest
    return f0, i0, j0, size


@njit(cache=True)
def _heuristic(lats, lons, i, j, goal_i, goal_j, max_speed_kmh):
    lat1 = lats[i]; lon1 = lons[j]
    lat2 = lats[goal_i]; lon2 = lons[goal_j]
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    dist_km = 2 * R * math.asin(math.sqrt(a))
    return dist_km / max_speed_kmh


@njit(cache=True)
def _astar_core(cost_stack, directions, lats, lons, start_i, start_j, goal_i, goal_j, max_speed_kmh):
    n_dirs = directions.shape[0]
    n_rows = cost_stack.shape[1]
    n_cols = cost_stack.shape[2]

    g_score = np.full((n_rows, n_cols), np.inf)
    came_i = np.full((n_rows, n_cols), -1, dtype=np.int32)
    came_j = np.full((n_rows, n_cols), -1, dtype=np.int32)

    capacity = n_rows * n_cols * 4
    heap_f = np.empty(capacity, dtype=np.float64)
    heap_i = np.empty(capacity, dtype=np.int32)
    heap_j = np.empty(capacity, dtype=np.int32)
    size = 0

    g_score[start_i, start_j] = 0.0
    size = _heap_push(heap_f, heap_i, heap_j, size, 0.0, start_i, start_j)

    found = False
    while size > 0:
        f, ci, cj, size = _heap_pop(heap_f, heap_i, heap_j, size)
        if ci == goal_i and cj == goal_j:
            found = True
            break

        current_g = g_score[ci, cj]

        for d in range(n_dirs):
            di = directions[d, 0]
            dj = directions[d, 1]
            ni = ci + di
            nj = cj + dj
            if ni < 0 or ni >= n_rows or nj < 0 or nj >= n_cols:
                continue
            cost = cost_stack[d, ci, cj]
            if not np.isfinite(cost):
                continue
            tentative_g = current_g + cost
            if tentative_g < g_score[ni, nj]:
                g_score[ni, nj] = tentative_g
                came_i[ni, nj] = ci
                came_j[ni, nj] = cj
                fscore = tentative_g + _heuristic(lats, lons, ni, nj, goal_i, goal_j, max_speed_kmh)
                if size >= capacity:
                    return came_i, came_j, False
                size = _heap_push(heap_f, heap_i, heap_j, size, fscore, ni, nj)

    return came_i, came_j, found


def astar_on_grid(cost_arrays: dict, lats: np.ndarray, lons: np.ndarray,
                    start_rc: tuple[int, int], goal_rc: tuple[int, int]):
    """
    Runs A* search over the grid, reading edge costs from cost_arrays.
    Returns the path as a list of (row, col) pairs from start to goal, or
    None if the goal is unreachable. Same signature and behavior as
    before — internally now dispatches to the Numba-jitted core.
    """
    n_dirs = len(DIRECTIONS)
    cost_stack = np.empty((n_dirs, len(lats), len(lons)), dtype=np.float64)
    for d, (di, dj) in enumerate(DIRECTIONS):
        cost_stack[d] = cost_arrays[(di, dj)]

    came_i, came_j, found = _astar_core(
        cost_stack, DIRECTIONS_ARR, lats, lons,
        start_rc[0], start_rc[1], goal_rc[0], goal_rc[1], TOBLER_MAX_SPEED_KMH
    )

    if not found:
        return None

    path = [goal_rc]
    ci, cj = goal_rc
    while came_i[ci, cj] != -1:
        ci, cj = int(came_i[ci, cj]), int(came_j[ci, cj])
        path.append((ci, cj))
    path.reverse()
    return path


def build_grid_state(bbox: tuple[float, float, float, float] = DEFAULT_TEST_BBOX,
                       cell_size_m: float = DEFAULT_CELL_SIZE_M,
                       verbose: bool = True) -> dict:
    """
    Performs the full setup for a region: fetches DEM tiles, builds the
    elevation grid, fetches and applies the water and stream masks, and
    precomputes the directional cost arrays. The returned state can be
    reused for routing between any number of waypoint pairs within this
    bounding box without repeating this work.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    mid_lat = (min_lat + max_lat) / 2
    zoom = zoom_for_resolution(cell_size_m, mid_lat)

    t0 = time.time()

    # Water/stream lookup only needs bbox, not the DEM mosaic, so it's
    # started immediately on a background thread rather than waiting for
    # the DEM fetch to finish first. get_water_and_stream_geometries tries
    # the local pre-fetched dataset first (near-instant) and only falls
    # back to a live Overpass call if that dataset hasn't been built yet
    # — see prefetch_nz_water_data.py. Either way, its result is collected
    # further down once lats/lons (needed to build the masks) exist.
    t_overpass_start = time.time()
    overpass_executor = ThreadPoolExecutor(max_workers=1)
    overpass_future = overpass_executor.submit(get_water_and_stream_geometries, bbox)

    mosaic, origin_x, origin_y = fetch_dem_mosaic(bbox, zoom)
    t1 = time.time()

    elevation_grid, lats, lons, lat_step, lon_step, n_rows, n_cols = build_elevation_grid(
        bbox, cell_size_m, mosaic, origin_x, origin_y, zoom
    )
    t2 = time.time()

    # Timed separately from the mask computation below (union/buffer/
    # contains_xy over the whole grid), since they have very different fix
    # strategies. water_lookup_s covers get_water_and_stream_geometries'
    # own total duration (from when it was submitted above) — with the
    # overlap, the wall-clock time this function actually spends waiting
    # on it can be much less than this number, or zero if it finished
    # during the DEM fetch. It's near-instant if the local dataset is
    # loaded (see prefetch_nz_water_data.py), or dominated by network
    # latency if falling back to a live Overpass call.
    water_lookup_s = 0.0
    mask_compute_s = 0.0
    try:
        water_polygons, stream_lines = overpass_future.result()
        overpass_executor.shutdown(wait=False)
        water_lookup_s = time.time() - t_overpass_start

        t_mask_start = time.time()
        water_mask = build_water_mask(water_polygons, lats, lons)
        stream_mask = build_stream_proximity_mask(stream_lines, lats, lons)
        mask_compute_s = time.time() - t_mask_start

        n_water_features = len(water_polygons)
        n_stream_features = len(stream_lines)
    except Exception as e:
        overpass_executor.shutdown(wait=False)
        water_mask = np.zeros((n_rows, n_cols), dtype=bool)
        stream_mask = np.zeros((n_rows, n_cols), dtype=bool)
        n_water_features = 0
        n_stream_features = 0
        print(f"WARNING: water/stream lookup failed ({e}) — routing without water "
              f"blocking or stream penalty.")
    t2b = time.time()

    cost_arrays = compute_directional_costs(elevation_grid, lats, lat_step, lon_step)
    apply_stream_proximity_penalty(cost_arrays, stream_mask)
    apply_water_mask(cost_arrays, water_mask)
    t3 = time.time()

    if verbose:
        n_blocked = int(water_mask.sum())
        n_stream_zone = int(stream_mask.sum())
        print(f"Grid build timing — fetch: {t1 - t0:.2f}s, sample: {t2 - t1:.3f}s, "
              f"water/stream wait after grid ready: {t2b - t2:.2f}s (lookup total: {water_lookup_s:.2f}s, "
              f"mask compute: {mask_compute_s:.2f}s; {n_water_features} water features, "
              f"{n_blocked} cells blocked; {n_stream_features} stream features, "
              f"{n_stream_zone} cells in proximity zone, of {n_rows * n_cols} total), "
              f"cost arrays: {t3 - t2b:.3f}s, total: {t3 - t0:.2f}s")

    return {
        "elevation_grid": elevation_grid,
        "lats": lats,
        "lons": lons,
        "cost_arrays": cost_arrays,
        "water_mask": water_mask,
        "stream_proximity_mask": stream_mask,
        "n_rows": n_rows,
        "n_cols": n_cols,
        "bbox": bbox,
        "cell_size_m": cell_size_m,
    }


def find_nearest_rc(lats: np.ndarray, lons: np.ndarray,
                      lon: float, lat: float) -> tuple[int, int]:
    """Snaps an arbitrary (lon, lat) coordinate to the nearest grid row and column."""
    i = int(np.argmin(np.abs(lats - lat)))
    j = int(np.argmin(np.abs(lons - lon)))
    return i, j


def route_between_waypoints(grid_state: dict,
                              waypoint_a: tuple[float, float],
                              waypoint_b: tuple[float, float]) -> dict:
    """
    Routes between two waypoints using an already-built grid state. This
    is the inexpensive per-request path: grid_state is built once and
    reused across many calls, and only the waypoint-snapping and A* search
    happen here.
    """
    lats, lons = grid_state["lats"], grid_state["lons"]
    cost_arrays = grid_state["cost_arrays"]
    elevation_grid = grid_state["elevation_grid"]

    start_rc = find_nearest_rc(lats, lons, waypoint_a[0], waypoint_a[1])
    goal_rc = find_nearest_rc(lats, lons, waypoint_b[0], waypoint_b[1])

    t0 = time.time()
    path_rc = astar_on_grid(cost_arrays, lats, lons, start_rc, goal_rc)
    print(f"Pathfinding: {time.time() - t0:.3f}s")

    if path_rc is None:
        return {"ok": False, "error": "No path found between those two points."}

    path_lonlat = [(lons[j], lats[i]) for i, j in path_rc]
    path_elevations = [float(elevation_grid[i, j]) for i, j in path_rc]

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
        "elevations_m": path_elevations,
        "distance_km": distance_km,
        "estimated_hours": total_hours,
        "climb_m": total_climb_m,
        "n_points": len(path_lonlat),
    }


# ── Dynamic bounding box and resolution — supports routing between any ──
# ── two points, rather than a single fixed test region                 ──
#
# New Zealand is far too large to grid at a fixed fine resolution — at a
# 15m cell size, a nationwide grid would require on the order of 1.2
# billion nodes. Instead, the grid's cell size adapts to each query: a
# fixed node budget bounds the grid, and cell size grows for longer
# queries. Very long-distance (coast-to-coast) queries degrade to a
# resolution too coarse to represent real trail-scale terrain, and a
# warning is printed in that case.

DEFAULT_NODE_BUDGET = 2_000_000
MIN_CELL_SIZE_M = 10.0
MAX_CELL_SIZE_M = 1_000.0
MIN_PADDING_DEG = 0.01


def compute_dynamic_bbox(waypoint_a: tuple[float, float],
                           waypoint_b: tuple[float, float],
                           padding_frac: float = 0.3) -> tuple[float, float, float, float]:
    """Builds a padded bounding box around two waypoints, giving the route room to deviate from a straight line."""
    return compute_dynamic_bbox_multi([waypoint_a, waypoint_b], padding_frac)


def compute_dynamic_bbox_multi(waypoints: list[tuple[float, float]],
                                 padding_frac: float = 0.3) -> tuple[float, float, float, float]:
    """Same as compute_dynamic_bbox, generalized to an arbitrary number of waypoints."""
    lons = [w[0] for w in waypoints]
    lats = [w[1] for w in waypoints]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)

    lon_pad = max((max_lon - min_lon) * padding_frac, MIN_PADDING_DEG)
    lat_pad = max((max_lat - min_lat) * padding_frac, MIN_PADDING_DEG)

    return (min_lat - lat_pad, min_lon - lon_pad, max_lat + lat_pad, max_lon + lon_pad)


def choose_cell_size_for_budget(bbox: tuple[float, float, float, float],
                                  node_budget: int = DEFAULT_NODE_BUDGET) -> float:
    """Chooses a cell size that keeps the bounding box's grid within node_budget total nodes, clamped to a sane range."""
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
    """Performs a free search between two waypoints anywhere in New Zealand, building a bounding box and grid sized to the query."""
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
              f"trail-scale terrain — treat this route as a rough estimate only.")

    grid_state = build_grid_state(bbox, cell_size_m)
    result = route_between_waypoints(grid_state, waypoint_a, waypoint_b)
    result["bbox_used"] = bbox
    result["cell_size_m_used"] = cell_size_m
    return result


def route_via_waypoints(waypoints: list[tuple[float, float]],
                          node_budget: int = DEFAULT_NODE_BUDGET,
                          padding_frac: float = 0.3) -> dict:
    """
    Routes through an ordered sequence of waypoints — for example, scenic
    stops placed by the user — using a single grid built to cover the
    entire chain. Each leg between consecutive waypoints is searched
    independently for its own lowest-cost path.
    """
    if len(waypoints) < 2:
        raise ValueError("Need at least 2 waypoints")

    bbox = compute_dynamic_bbox_multi(waypoints, padding_frac)
    cell_size_m = choose_cell_size_for_budget(bbox, node_budget)
    print(f"Multi-waypoint bbox for {len(waypoints)} waypoints, cell size: {cell_size_m:.1f}m")

    grid_state = build_grid_state(bbox, cell_size_m)

    total_distance_km = 0.0
    total_hours = 0.0
    total_climb_m = 0.0
    full_coords = []
    full_elevations = []
    for leg_num, (a, b) in enumerate(zip(waypoints[:-1], waypoints[1:]), start=1):
        leg = route_between_waypoints(grid_state, a, b)
        if not leg["ok"]:
            return {"ok": False, "error": f"Leg {leg_num} ({a} -> {b}) failed: {leg['error']}"}
        total_distance_km += leg["distance_km"]
        total_hours += leg["estimated_hours"]
        total_climb_m += leg["climb_m"]
        full_coords.extend(leg["route"]["geometry"]["coordinates"])
        full_elevations.extend(leg["elevations_m"])
        print(f"  Leg {leg_num}: {leg['distance_km']:.2f}km, "
              f"{leg['estimated_hours']:.2f}h, {leg['climb_m']:.1f}m climb")

    return {
        "ok": True,
        "distance_km": total_distance_km,
        "estimated_hours": total_hours,
        "climb_m": total_climb_m,
        "n_points": len(full_coords),
        "route": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": full_coords},
        },
        "elevations_m": full_elevations,
        "bbox_used": bbox,
        "cell_size_m_used": cell_size_m,
    }


def route_with_waypoints(waypoint_a: tuple[float, float],
                           waypoint_b: tuple[float, float],
                           via: list[tuple[float, float]] | None = None,
                           node_budget: int = DEFAULT_NODE_BUDGET,
                           padding_frac: float = 0.3) -> dict:
    """
    Main routing entry point, used by both the API and the CLI. When via
    is None or empty, this delegates directly to route_any_two_points — a
    free search with no intermediate constraints. When via contains one or
    more waypoints, the route is required to pass through waypoint_a, each
    via point in order, and then waypoint_b — useful for a user-placed
    detour that the lowest-cost route would not otherwise take.
    """
    if not via:
        return route_any_two_points(waypoint_a, waypoint_b, node_budget=node_budget,
                                      padding_frac=padding_frac)

    all_waypoints = [waypoint_a] + list(via) + [waypoint_b]
    return route_via_waypoints(all_waypoints, node_budget=node_budget, padding_frac=padding_frac)


# ── GPX export ───────────────────────────────────────────────────────────
#
# GPX (GPS Exchange Format) is what most hiking/GPS apps and devices
# actually import, unlike raw GeoJSON — worth offering as the primary
# output format. It's plain XML, so no extra dependency is needed beyond
# the standard library's xml.etree.ElementTree.

def route_to_gpx(result: dict, track_name: str = "Ridge Walker Route") -> str:
    """
    Converts a successful route_with_waypoints()/route_between_waypoints()
    result into a GPX 1.1 XML string. Includes per-point elevation
    (<ele>) when available — this is why route_between_waypoints() and
    route_via_waypoints() now return "elevations_m" alongside the route
    coordinates, not just the bare GeoJSON LineString.

    Returns the GPX document as a string — write it to a .gpx file
    yourself, or use write_gpx_file() below to do that directly.
    """
    if not result.get("ok"):
        raise ValueError("Cannot export a failed routing result to GPX.")

    coordinates = result["route"]["geometry"]["coordinates"]
    elevations = result.get("elevations_m")
    if elevations is not None and len(elevations) != len(coordinates):
        # Shouldn't happen given how these are built together above, but
        # fail safe rather than silently mismatching points to elevations.
        elevations = None

    gpx = ET.Element("gpx", attrib={
        "version": "1.1",
        "creator": "RidgeWalker",
        "xmlns": "http://www.topografix.com/GPX/1/1",
    })
    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = track_name
    trkseg = ET.SubElement(trk, "trkseg")

    for idx, coord in enumerate(coordinates):
        lon, lat = coord[0], coord[1]  # tolerate a 3rd (elevation) value now embedded in coordinates
        trkpt = ET.SubElement(trkseg, "trkpt", attrib={"lat": f"{lat:.7f}", "lon": f"{lon:.7f}"})
        if elevations is not None:
            ET.SubElement(trkpt, "ele").text = f"{elevations[idx]:.1f}"

    ET.indent(gpx, space="  ")  # pretty-print (Python 3.9+)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(gpx, encoding="unicode")


def write_gpx_file(result: dict, path: str, track_name: str = "Ridge Walker Route") -> None:
    """Writes a routing result to a .gpx file. See route_to_gpx() for the underlying conversion."""
    gpx_str = route_to_gpx(result, track_name=track_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(gpx_str)


# ── Flask API ─────────────────────────────────────────────────────────
#
# Exposes POST /route, accepting {"a": [lon, lat], "b": [lon, lat],
# "via": [[lon, lat], ...]}. The "via" field is optional. The grid is
# built fresh for each request, since the region required depends on the
# specific query.

def create_app(node_budget: int = DEFAULT_NODE_BUDGET):
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.route("/route", methods=["POST"])
    def route():
        body = request.get_json(force=True)
        a = tuple(body["a"])
        b = tuple(body["b"])
        via = [tuple(p) for p in body.get("via", [])] or None
        result = route_with_waypoints(a, b, via=via, node_budget=node_budget)
        if result.get("ok"):
            result["gpx"] = route_to_gpx(result)
        return jsonify(result)

    return app


# ── AWS Lambda entry point ───────────────────────────────────────────
#
# Deployed as its own Lambda function behind API Gateway (POST /route),
# accepting the same request body as the Flask endpoint above. The
# function timeout should be set to 30 seconds or more, and memory
# increased beyond the default, since NumPy performs poorly under
# Lambda's default 128MB allocation.

CORS_HEADERS = {
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    try:
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        payload = json.loads(raw_body)

        waypoint_a = tuple(payload["a"])
        waypoint_b = tuple(payload["b"])
        via = [tuple(p) for p in payload.get("via", [])] or None
        node_budget = int(payload.get("node_budget", DEFAULT_NODE_BUDGET))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        return {
            "statusCode": 400,
            "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": f"Bad request: {e}"}),
        }

    try:
        result = route_with_waypoints(waypoint_a, waypoint_b, via=via, node_budget=node_budget)
        if result.get("ok"):
            # Hand the frontend the exact, already-tested GPX output rather
            # than making it reconstruct one from raw coordinates — same
            # route_to_gpx() the CLI's --out uses, just returned in the API
            # response instead of written to a local file.
            result["gpx"] = route_to_gpx(result)
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": f"Routing failed: {e}"}),
        }

    return {
        "statusCode": 200 if result.get("ok") else 400,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(result),
    }


if __name__ == "__main__":
    import argparse
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
    parser.add_argument("--out", type=str, default="terrain_route.gpx",
                         help="Output GPX file path (default terrain_route.gpx)")
    parser.add_argument("--via", type=str, default=None,
                         help="Semicolon-separated 'lon,lat' waypoints to route through, "
                              "e.g. --via '175.6345,-39.14309;175.65034,-39.13563'")

    args = parser.parse_args()

    waypoint_a = (args.lon_a, args.lat_a)
    waypoint_b = (args.lon_b, args.lat_b)
    via_points = None
    if args.via:
        via_points = [tuple(map(float, pair.split(","))) for pair in args.via.split(";")]

    result = route_with_waypoints(waypoint_a, waypoint_b, via=via_points, node_budget=args.node_budget)

    if not result["ok"]:
        print("Routing failed:", result["error"])
        sys.exit(1)

    print(f"\nRoute found: {result['distance_km']:.2f} km, "
          f"~{result['estimated_hours']:.2f} hours, "
          f"{result['climb_m']:.1f} m climb, {result['n_points']} points.")

    write_gpx_file(result, args.out)
    print(f"Saved route to {args.out}")
