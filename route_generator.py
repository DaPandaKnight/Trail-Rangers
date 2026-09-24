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
import re
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
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            # NEVER let the raw exception propagate — requests' default
            # HTTPError message includes the full request URL, which
            # embeds the LINZ API key as a plain query parameter. That
            # message was reaching end users verbatim through the API's
            # error response. `from None` deliberately severs the
            # exception chain so even a traceback of THIS exception can't
            # show the original (key-containing) one via Python's
            # "during handling of the above exception" chaining.
            safe_url = url.replace(api_key, "***REDACTED***")
            raise RuntimeError(
                f"DEM tile fetch failed ({type(e).__name__}) for {safe_url}"
            ) from None
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
    from) into memory and builds STRtree spatial indexes (used for the
    per-bbox feature COUNT shown in logging, and for the Overpass-fallback
    code path).

    The global water union and stream buffer union are precomputed
    OFFLINE by prefetch_nz_water_data.py and loaded directly here, rather
    than computed at cold-start — see that script's main() for why: a
    real production run showed the stream buffer union alone taking 677
    seconds (over 11 minutes) at cold-start, almost certainly enough to
    exceed a typical Lambda timeout and fail the function outright. That
    work has to happen offline, not on a live request's critical path.

    Bridge crossings (fix #3) are handled the same way — see
    prefetch_nz_water_data.py's parse_bridge_lines(). Pickles built
    before this feature existed (4-tuple or 2-tuple) simply have no
    bridge data; routing falls back to treating all water as fully
    blocked, exactly as before this feature was added.

    Cached at module level — this whole function runs at most once per
    process (once per Lambda cold start), not once per request.
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
        loaded = pickle.load(f)

    bridge_lines = []
    global_bridge_buffer_union = None

    if len(loaded) == 6:
        (water_polygons, stream_lines, global_water_union, global_stream_buffer_union,
         bridge_lines, global_bridge_buffer_union) = loaded
    elif len(loaded) == 4:
        water_polygons, stream_lines, global_water_union, global_stream_buffer_union = loaded
        print("NOTE: this nz_water_data.pkl predates bridge crossings (fix #3) — "
              "routing without bridge awareness. Re-run prefetch_nz_water_data.py "
              "to enable crossing real mapped bridges over otherwise-blocked water.")
    else:
        # Old-format pickle (from before unions were precomputed offline) —
        # fall back to computing them here, with a clear warning that this
        # is the slow path this whole change was meant to avoid. Re-run
        # prefetch_nz_water_data.py to get the fast, offline-precomputed
        # version instead of hitting this every cold start.
        water_polygons, stream_lines = loaded
        print("WARNING: this nz_water_data.pkl is in the OLD format (no precomputed "
              "unions, no bridge data) — computing unions now, which may take a long "
              "time (a real run measured 677s for the stream buffer alone). Re-run "
              "prefetch_nz_water_data.py to save the precomputed version and enable "
              "bridge crossings.")

        t_union_start = time.time()
        global_water_union = unary_union(water_polygons) if water_polygons else None
        print(f"Computed global water union in {time.time() - t_union_start:.2f}s.")

        NZ_REPRESENTATIVE_LAT = -41.0
        global_stream_buffer_union = None
        if stream_lines:
            t_buffer_start = time.time()
            merged_streams = unary_union(stream_lines)
            m_per_deg_lat, m_per_deg_lon = meters_per_degree(NZ_REPRESENTATIVE_LAT)
            buffer_deg = STREAM_BUFFER_M / min(m_per_deg_lat, m_per_deg_lon)
            global_stream_buffer_union = merged_streams.buffer(buffer_deg)
            print(f"Computed global stream buffer union in {time.time() - t_buffer_start:.2f}s.")

    water_tree = STRtree(water_polygons) if water_polygons else None
    stream_tree = STRtree(stream_lines) if stream_lines else None
    bridge_tree = STRtree(bridge_lines) if bridge_lines else None

    print(f"Loaded local water dataset: {len(water_polygons)} water polygons, "
          f"{len(stream_lines)} stream lines, {len(bridge_lines)} bridge crossings. "
          f"No Overpass calls needed for routing now.")

    _local_water_index = {
        "water_tree": water_tree, "water_polygons": water_polygons,
        "stream_tree": stream_tree, "stream_lines": stream_lines,
        "bridge_tree": bridge_tree, "bridge_lines": bridge_lines,
        "global_water_union": global_water_union,
        "global_stream_buffer_union": global_stream_buffer_union,
        "global_bridge_buffer_union": global_bridge_buffer_union,
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


def query_local_bridge_data(bbox: tuple[float, float, float, float]) -> list[LineString]:
    """
    Returns bridge crossing lines (fix #3) intersecting bbox, or an empty
    list if no local dataset is available or it predates bridge support
    — unlike water/stream, there's no live-Overpass fallback for bridges;
    this is a purely additive feature, so "no bridge data" just means
    routing falls back to treating all water as fully blocked, same as
    before this feature existed. Pure in-memory spatial query, no
    network call.
    """
    index = _load_local_water_index()
    if index is None or index.get("bridge_tree") is None:
        return []

    min_lat, min_lon, max_lat, max_lon = bbox
    query_box = box(min_lon, min_lat, max_lon, max_lat)

    result = []
    for idx in index["bridge_tree"].query(query_box):
        geom = index["bridge_lines"][idx]
        if geom.intersects(query_box):
            result.append(geom)
    return result


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
                       lons: np.ndarray):
    """
    Rasterizes water polygons onto the grid. Returns (merged_geometry,
    water_mask) — the merged geometry is needed by apply_water_mask's
    midpoint check (see its docstring), not just the rasterized mask.
    """
    n_rows, n_cols = len(lats), len(lons)
    if not water_polygons:
        return None, np.zeros((n_rows, n_cols), dtype=bool)

    merged = unary_union(water_polygons)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return merged, shapely.contains_xy(merged, lon_grid, lat_grid)


def rasterize_precomputed_geometry(merged_geometry, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """
    Same rasterization as build_water_mask()/the contains_xy step inside
    build_stream_proximity_mask(), but for a geometry that's ALREADY
    merged (see _load_local_water_index's precomputed global unions) —
    skips the expensive unary_union() call entirely, since that's the
    confirmed bottleneck at country-spanning bbox scale, not the
    rasterization itself (which was already shown fast — see
    _load_local_water_index's docstring for the real numbers).
    """
    n_rows, n_cols = len(lats), len(lons)
    if merged_geometry is None:
        return np.zeros((n_rows, n_cols), dtype=bool)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return shapely.contains_xy(merged_geometry, lon_grid, lat_grid)


def _compute_candidate_crossing_region(merged_geometry, lats: np.ndarray, lons: np.ndarray,
                                          buffer_cells: float = 2.0) -> np.ndarray:
    """
    Cheap pre-filter for _compute_step_crossing_blocked: buffers the
    ACTUAL underlying geometry — not the already-rasterized water_mask —
    by roughly buffer_cells grid cells, then rasterizes the buffered
    result. Any cell where this comes back False cannot possibly have a
    crossing-check hit for ANY direction, since even a razor-thin sliver
    of real geometry, once buffered by more than a cell's width, becomes
    wide enough to register in a coarse cell-center rasterization.

    THIS IS SOUND WHERE AN EARLIER ATTEMPT WASN'T: that attempt dilated
    the already-rasterized water_mask as its "near water" test, which is
    exactly backwards — the whole point of the crossing check is to catch
    water narrow enough that water_mask can be entirely empty (no cell
    center falls inside it), so dilating an all-False mask just gives
    back all-False, silently defeating the fix in precisely the case it
    exists for. Buffering the SOURCE geometry first avoids that: a
    razor-thin polygon has no interior cell centers, but after a
    generous buffer it certainly does.
    """
    n_rows, n_cols = len(lats), len(lons)
    if merged_geometry is None:
        return np.zeros((n_rows, n_cols), dtype=bool)

    lat_step = abs(lats[1] - lats[0]) if n_rows > 1 else 0.0
    lon_step = abs(lons[1] - lons[0]) if n_cols > 1 else 0.0
    buffer_deg = buffer_cells * max(lat_step, lon_step)

    buffered = merged_geometry.buffer(buffer_deg)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return shapely.contains_xy(buffered, lon_grid, lat_grid)


def _compute_step_crossing_blocked(merged_geometry, lats: np.ndarray, lons: np.ndarray,
                                     di: int, dj: int, n_samples: int = 3,
                                     candidate_region: np.ndarray = None) -> np.ndarray:
    """
    For direction (di, dj), checks several points ALONG the step from
    each cell (i,j) to its neighbor (i+di, j+dj) against merged_geometry
    — catching a real bug the endpoint-only check misses: a knight's-move
    step can span up to ~22m at 10m resolution, so a water gap NARROWER
    than the step can sit entirely between two dry cell centers, letting
    a route silently "jump" over real water. Grid rows/cols map to
    lat/lon via simple linear interpolation, since lats/lons are evenly
    spaced (see build_elevation_grid) — exact, not an approximation.

    SAMPLES MULTIPLE POINTS, NOT JUST THE MIDPOINT: an earlier version
    checked only the single midpoint and looked correct on a simple
    1-cell step, but testing caught a real gap — for the longer
    knight's-move directions (spanning 2 cells), a narrow obstacle
    doesn't have to sit at the exact arithmetic center of the step, and
    a single sample missed one sitting between the two cells but off the
    step's exact midpoint. n_samples=3 (at 1/4, 1/2, 3/4 along the step)
    substantially closes that gap; it is not an absolute mathematical
    guarantee against an adversarially-thin sliver landing exactly
    between two sample points, but that residual risk is far smaller
    than the confirmed gap this replaces. A fully rigorous fix would be a
    true line-segment-vs-polygon intersection test per edge, not sampling
    at all — noted here as a possible future refinement if this residual
    risk ever turns out to matter in practice.

    candidate_region (see _compute_candidate_crossing_region) restricts
    the expensive per-sample geometry query to cells that could actually
    matter — measured to cut this from ~10s to a small fraction of that
    on a 2-million-node grid with real-density water polygons, without
    losing any correctness (unlike an earlier, unsound attempt that
    restricted based on the lossy rasterized mask instead of the source
    geometry — see that function's docstring).
    """
    n_rows, n_cols = len(lats), len(lons)
    if merged_geometry is None:
        return np.zeros((n_rows, n_cols), dtype=bool)

    if candidate_region is not None and not candidate_region.any():
        return np.zeros((n_rows, n_cols), dtype=bool)

    lat_step = lats[1] - lats[0] if n_rows > 1 else 0.0
    lon_step = lons[1] - lons[0] if n_cols > 1 else 0.0

    row_idx = np.arange(n_rows)
    col_idx = np.arange(n_cols)

    blocked = np.zeros((n_rows, n_cols), dtype=bool)
    for k in range(1, n_samples + 1):
        t = k / (n_samples + 1)  # e.g. n_samples=3 -> t in {0.25, 0.5, 0.75}
        sample_row = row_idx + di * t
        sample_col = col_idx + dj * t

        sample_lat_1d = lats[0] + sample_row * lat_step
        sample_lon_1d = lons[0] + sample_col * lon_step
        sample_lat_grid = np.broadcast_to(sample_lat_1d[:, None], (n_rows, n_cols))
        sample_lon_grid = np.broadcast_to(sample_lon_1d[None, :], (n_rows, n_cols))

        if candidate_region is None:
            blocked |= shapely.contains_xy(merged_geometry, sample_lon_grid, sample_lat_grid)
        else:
            hits = np.zeros((n_rows, n_cols), dtype=bool)
            hits[candidate_region] = shapely.contains_xy(
                merged_geometry, sample_lon_grid[candidate_region], sample_lat_grid[candidate_region]
            )
            blocked |= hits

    return blocked


def apply_water_mask(cost_arrays: dict, water_mask: np.ndarray,
                       lats: np.ndarray = None, lons: np.ndarray = None,
                       merged_water_geometry=None, local_water_geometry=None) -> dict:
    """
    Sets the cost of any edge that touches a water cell to infinity,
    hard-blocking it from the route.

    Checking only the two endpoint cells (as before) misses a real bug:
    a knight's-move step can be up to ~22m at 10m resolution, so a water
    gap NARROWER than the step can sit entirely between two dry cell
    centers, letting the path silently jump over real water a hiker
    could never actually cross there. When lats/lons and
    merged_water_geometry are provided, this also checks several points
    ALONG each candidate step against the actual (un-rasterized) water
    geometry, catching gaps the endpoint-only check would miss — see
    _compute_step_crossing_blocked's docstring for why multiple sample
    points, not just one.

    merged_water_geometry vs. local_water_geometry — these are
    DELIBERATELY separate, not redundant:
    - merged_water_geometry is used for the actual contains_xy crossing
      checks. It's fine for this to be the enormous precomputed NATIONAL
      union (all 61,855 water polygons) — contains_xy scales with query
      point count, not geometry complexity.
    - local_water_geometry is what the candidate-region pre-filter
      buffers (see _compute_candidate_crossing_region). This MUST be a
      small, per-bbox-local geometry, not the national union — .buffer()
      genuinely scales with geometry complexity, and buffering the whole
      country's water data on every single request caused a real
      multi-minute hang in production. If omitted, falls back to
      merged_water_geometry (correct but potentially catastrophically
      slow if that happens to be a large precomputed union — always pass
      a local one when the caller has access to a bbox-scoped subset).
    """
    water_float = water_mask.astype(np.float64)
    check_crossings = (merged_water_geometry is not None and lats is not None and lons is not None)
    buffer_source = local_water_geometry if local_water_geometry is not None else merged_water_geometry

    candidate_region = None
    if check_crossings:
        candidate_region = _compute_candidate_crossing_region(buffer_source, lats, lons)

    for di, dj in DIRECTIONS:
        neighbor_is_water = shifted(water_float, di, dj)
        neighbor_is_water = np.nan_to_num(neighbor_is_water, nan=0.0) >= 0.5
        blocked = water_mask | neighbor_is_water

        if check_crossings:
            crossing_blocked = _compute_step_crossing_blocked(
                merged_water_geometry, lats, lons, di, dj, candidate_region=candidate_region
            )
            blocked = blocked | crossing_blocked

        cost_arrays[(di, dj)] = np.where(blocked, np.inf, cost_arrays[(di, dj)])
    return cost_arrays


def build_stream_proximity_mask(stream_lines: list[LineString], lats: np.ndarray,
                                  lons: np.ndarray, buffer_m: float = STREAM_BUFFER_M):
    """
    Rasterizes a buffer_m-wide corridor around stream_lines onto the
    grid. Returns (buffered_geometry, stream_mask) — the buffered
    geometry is needed by apply_stream_proximity_penalty's crossing
    check (see its docstring), not just the rasterized mask.
    """
    n_rows, n_cols = len(lats), len(lons)
    if not stream_lines:
        return None, np.zeros((n_rows, n_cols), dtype=bool)

    merged = unary_union(stream_lines)
    mid_lat = float(np.mean(lats))
    m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)
    buffer_deg = buffer_m / min(m_per_deg_lat, m_per_deg_lon)
    buffered = merged.buffer(buffer_deg)

    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return buffered, shapely.contains_xy(buffered, lon_grid, lat_grid)


def apply_stream_proximity_penalty(cost_arrays: dict, stream_mask: np.ndarray,
                                     weight: float = STREAM_PROXIMITY_WEIGHT,
                                     lats: np.ndarray = None, lons: np.ndarray = None,
                                     merged_stream_geometry=None, local_stream_geometry=None) -> dict:
    """
    Multiplies the cost of any edge within the stream-proximity buffer by
    weight. This is a soft penalty, not a hard block.

    Same crossing-check fix as apply_water_mask (see its docstring for
    the full rationale) — a step that jumps clean over a stream's buffer
    zone without either endpoint touching it would otherwise silently
    dodge the soft penalty entirely, not just the hard-block case.

    local_stream_geometry: same reasoning as apply_water_mask's
    local_water_geometry — the candidate-region pre-filter's .buffer()
    call must run on a small, per-bbox-local geometry, never the
    enormous precomputed national stream union (93,891 lines), which
    caused a real multi-minute hang in production when buffered
    per-request.
    """
    if weight == 1.0:
        return cost_arrays

    stream_float = stream_mask.astype(np.float64)
    check_crossings = (merged_stream_geometry is not None and lats is not None and lons is not None)
    buffer_source = local_stream_geometry if local_stream_geometry is not None else merged_stream_geometry

    candidate_region = None
    if check_crossings:
        candidate_region = _compute_candidate_crossing_region(buffer_source, lats, lons)

    for di, dj in DIRECTIONS:
        neighbor_in_zone = shifted(stream_float, di, dj)
        neighbor_in_zone = np.nan_to_num(neighbor_in_zone, nan=0.0) >= 0.5
        in_zone = stream_mask | neighbor_in_zone

        if check_crossings:
            crossing_in_zone = _compute_step_crossing_blocked(
                merged_stream_geometry, lats, lons, di, dj, candidate_region=candidate_region
            )
            in_zone = in_zone | crossing_in_zone

        cost_arrays[(di, dj)] = np.where(in_zone, cost_arrays[(di, dj)] * weight, cost_arrays[(di, dj)])
    return cost_arrays


def apply_bridge_crossings(cost_arrays: dict, original_cost_arrays: dict,
                             lats: np.ndarray, lons: np.ndarray,
                             merged_bridge_geometry, local_bridge_geometry=None) -> dict:
    """
    Fix #3: restores the ORIGINAL (pre-water-masking) cost for any edge
    that crosses a real, mapped bridge — even though apply_water_mask
    already set it to infinity. This is deliberately an "allow-list on
    top of a block", applied AFTER water/stream masking, not a way of
    deciding fords are safe: per the river-crossing safety research that
    shaped this design, real danger (current speed, depth right now,
    recent rain) can't be judged from static map data, so the only
    crossing ever treated as legitimate is a real, physical bridge — a
    fact, not an inference.

    original_cost_arrays must be a snapshot taken BEFORE
    apply_water_mask/apply_stream_proximity_penalty ran (see
    build_grid_state) — restoring to that snapshot, rather than to some
    made-up "bridge cost", means walking a bridge costs exactly what the
    terrain/distance model already says that step should cost, nothing
    more or less.

    Reuses _compute_step_crossing_blocked and
    _compute_candidate_crossing_region — same multi-sample-along-the-step
    logic used to detect a water crossing, just applied to bridge
    geometry instead, and used to ALLOW rather than block. Same local-
    geometry-for-buffering requirement as apply_water_mask — see its
    docstring and build_grid_state's clipping step for why merged_bridge_geometry
    (which is fine to be a national precomputed union for the cheap
    contains_xy checks) must NOT be what gets passed to the buffer step.
    """
    check_crossings = (merged_bridge_geometry is not None and lats is not None and lons is not None)
    if not check_crossings:
        return cost_arrays

    buffer_source = local_bridge_geometry if local_bridge_geometry is not None else merged_bridge_geometry
    candidate_region = _compute_candidate_crossing_region(buffer_source, lats, lons)

    for di, dj in DIRECTIONS:
        on_bridge = _compute_step_crossing_blocked(
            merged_bridge_geometry, lats, lons, di, dj, candidate_region=candidate_region
        )
        cost_arrays[(di, dj)] = np.where(on_bridge, original_cost_arrays[(di, dj)], cost_arrays[(di, dj)])

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

    # Bridge crossings (fix #3) are fetched below, AFTER the water/stream
    # future resolves — not here. Both ultimately call the same shared
    # _load_local_water_index() cache; calling it concurrently from here
    # (main thread) while get_water_and_stream_geometries loads it on the
    # background thread is a real race — the guard flag can be set
    # before the data is actually populated, so a badly-timed concurrent
    # call sees "already attempted" and gets back None/empty, even though
    # the background thread finishes loading correctly moments later.
    # Confirmed this exact failure mode by testing before shipping it.

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

        # Safe to call now — _local_water_index is guaranteed populated
        # at this point (overpass_future.result() already returned,
        # meaning get_water_and_stream_geometries's call chain has
        # finished loading it), unlike calling this earlier alongside
        # the concurrent DEM fetch, which raced with the load itself.
        bridge_lines = query_local_bridge_data(bbox)

        t_mask_start = time.time()
        # By the time overpass_future has resolved, _load_local_water_index()
        # has already run as a side effect of get_water_and_stream_geometries
        # -> query_local_water_data, so _local_water_index is safe to read
        # here directly. If it's populated, use the precomputed global
        # unions (fast path — see _load_local_water_index's docstring for
        # why this matters, confirmed with real production numbers). If
        # it's None, the local dataset isn't available and this request
        # fell back to live Overpass — there's no precomputed global union
        # for an arbitrary per-request Overpass result, so union fresh,
        # exactly as before.
        if _local_water_index is not None:
            water_geometry = _local_water_index["global_water_union"]
            stream_geometry = _local_water_index["global_stream_buffer_union"]
            water_mask = rasterize_precomputed_geometry(water_geometry, lats, lons)
            stream_mask = rasterize_precomputed_geometry(stream_geometry, lats, lons)

            # CRITICAL, SECOND FIX: unioning water_polygons/stream_lines
            # directly (as a first attempt at this fix did) is still not
            # safe — the per-bbox filter in query_local_water_data
            # matches on BOUNDING BOX intersection, so a single real
            # river or coastline feature spanning many kilometers (and
            # thousands of vertices) can be pulled in WHOLE just for
            # passing near this bbox at one point, even though the
            # FEATURE COUNT stays small. Confirmed directly: a single
            # 5000-vertex LineString among just 32 total features took
            # 28.79s to buffer on its own — NZ's real rivers can be far
            # longer/more complex than that synthetic test, and this is
            # what caused a real 356-second hang in production despite
            # the "small union" fix above. CLIPPING each feature to the
            # query bbox (with a little padding) before unioning bounds
            # the actual vertex complexity that ever reaches .buffer(),
            # regardless of how long the original mapped feature is.
            min_lat, min_lon, max_lat, max_lon = bbox
            clip_pad = max(lat_step, lon_step) * 5  # a few cells of padding
            clip_box = box(min_lon - clip_pad, min_lat - clip_pad,
                             max_lon + clip_pad, max_lat + clip_pad)
            clipped_water = [p.intersection(clip_box) for p in water_polygons]
            clipped_water = [p for p in clipped_water if not p.is_empty]
            clipped_streams = [l.intersection(clip_box) for l in stream_lines]
            clipped_streams = [l for l in clipped_streams if not l.is_empty]
            local_water_union = unary_union(clipped_water) if clipped_water else None
            local_stream_union = unary_union(clipped_streams) if clipped_streams else None
        else:
            water_geometry, water_mask = build_water_mask(water_polygons, lats, lons)
            stream_geometry, stream_mask = build_stream_proximity_mask(stream_lines, lats, lons)
            # Already small (per-bbox) in this branch — safe to reuse directly.
            local_water_union = water_geometry
            local_stream_union = stream_geometry
        mask_compute_s = time.time() - t_mask_start

        n_water_features = len(water_polygons)
        n_stream_features = len(stream_lines)
    except Exception as e:
        overpass_executor.shutdown(wait=False)
        water_mask = np.zeros((n_rows, n_cols), dtype=bool)
        stream_mask = np.zeros((n_rows, n_cols), dtype=bool)
        water_geometry = None
        stream_geometry = None
        local_water_union = None
        local_stream_union = None
        bridge_lines = []
        n_water_features = 0
        n_stream_features = 0
        print(f"WARNING: water/stream lookup failed ({e}) — routing without water "
              f"blocking or stream penalty.")
        # This catch-all was hiding exactly which line raised an internal
        # bug behind a bare str(e) — fine for an EXPECTED external
        # failure (a real Overpass network error), useless for debugging
        # an internal one. Print the full traceback too, so a bug like
        # this is diagnosable from the very next run instead of guessing.
        import traceback
        traceback.print_exc()
    t2b = time.time()

    cost_arrays = compute_directional_costs(elevation_grid, lats, lat_step, lon_step)

    # Snapshot BEFORE any water/stream masking — apply_bridge_crossings
    # restores TO this, so walking a bridge costs exactly what the
    # terrain/distance model already says, not some made-up "bridge
    # cost". Must be a deep-ish copy per direction array, not the same
    # arrays that are about to be mutated in place below.
    original_cost_arrays = {k: v.copy() for k, v in cost_arrays.items()}

    apply_stream_proximity_penalty(cost_arrays, stream_mask, lats=lats, lons=lons,
                                     merged_stream_geometry=stream_geometry,
                                     local_stream_geometry=local_stream_union)
    apply_water_mask(cost_arrays, water_mask, lats=lats, lons=lons,
                       merged_water_geometry=water_geometry,
                       local_water_geometry=local_water_union)

    # Fix #3: restore bridge crossings AFTER water masking, not before —
    # this is deliberately an allow-list layered on top of the block, not
    # a way of avoiding it in the first place. Same clip-before-buffer
    # requirement as water/stream (see build_grid_state's water/stream
    # clipping comment above) — bridges are far fewer nationally, but the
    # exact same class of mistake (buffering an unclipped, potentially
    # long feature) applies equally here, so it's applied proactively
    # rather than waiting to find out the hard way a second time.
    n_bridge_features = len(bridge_lines)
    if bridge_lines:
        min_lat_b, min_lon_b, max_lat_b, max_lon_b = bbox
        bridge_clip_pad = max(lat_step, lon_step) * 5
        bridge_clip_box = box(min_lon_b - bridge_clip_pad, min_lat_b - bridge_clip_pad,
                                max_lon_b + bridge_clip_pad, max_lat_b + bridge_clip_pad)
        clipped_bridges = [l.intersection(bridge_clip_box) for l in bridge_lines]
        clipped_bridges = [l for l in clipped_bridges if not l.is_empty]
        local_bridge_union = unary_union(clipped_bridges) if clipped_bridges else None

        merged_bridge_geometry = None
        if _local_water_index is not None:
            merged_bridge_geometry = _local_water_index.get("global_bridge_buffer_union")
        if merged_bridge_geometry is None:
            # No precomputed national bridge buffer available (e.g. an
            # old-format pickle) — the local (already clipped, already
            # small) union is a fine, safe substitute for the
            # correctness check too in this fallback case.
            merged_bridge_geometry = local_bridge_union

        apply_bridge_crossings(cost_arrays, original_cost_arrays, lats, lons,
                                 merged_bridge_geometry, local_bridge_geometry=local_bridge_union)
    t3 = time.time()

    if verbose:
        n_blocked = int(water_mask.sum())
        n_stream_zone = int(stream_mask.sum())
        print(f"Grid build timing — fetch: {t1 - t0:.2f}s, sample: {t2 - t1:.3f}s, "
              f"water/stream wait after grid ready: {t2b - t2:.2f}s (lookup total: {water_lookup_s:.2f}s, "
              f"mask compute: {mask_compute_s:.2f}s; {n_water_features} water features, "
              f"{n_blocked} cells blocked; {n_stream_features} stream features, "
              f"{n_stream_zone} cells in proximity zone; {n_bridge_features} bridge crossings, "
              f"of {n_rows * n_cols} total), "
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
                              waypoint_b: tuple[float, float],
                              label_a: str = "Start point",
                              label_b: str = "End point") -> dict:
    """
    Routes between two waypoints using an already-built grid state. This
    is the inexpensive per-request path: grid_state is built once and
    reused across many calls, and only the waypoint-snapping and A* search
    happen here.

    label_a/label_b name waypoint_a/waypoint_b in the water-blocked error
    message below. Default to "Start point"/"End point" for the plain
    2-point case; route_via_waypoints passes more specific labels (e.g.
    "Waypoint 2") for each leg of a multi-waypoint chain, so a blocked
    waypoint in the middle of the sequence doesn't get reported as a
    generic "Start point"/"End point" of whichever leg happened to touch
    it — see route_via_waypoints for how those labels are chosen.
    """
    lats, lons = grid_state["lats"], grid_state["lons"]
    cost_arrays = grid_state["cost_arrays"]
    elevation_grid = grid_state["elevation_grid"]
    water_mask = grid_state.get("water_mask")

    start_rc = find_nearest_rc(lats, lons, waypoint_a[0], waypoint_a[1])
    goal_rc = find_nearest_rc(lats, lons, waypoint_b[0], waypoint_b[1])

    # Checked BEFORE running A* — a waypoint that itself snaps to a
    # mapped water/snow cell is a fundamentally different failure than
    # "no path found": expanding the search area can never fix it (the
    # point doesn't move), so retry_worthy=False tells the caller's
    # retry loop to stop immediately rather than burn 2 more expensive
    # attempts on something a bigger box was never going to solve. Also
    # skips a wasted A* call entirely for this case.
    if water_mask is not None:
        if water_mask[start_rc]:
            return {
                "ok": False,
                "error": f"{label_a} falls within a mapped water or snow/ice body — pick a point on dry land.",
                "retry_worthy": False,
            }
        if water_mask[goal_rc]:
            return {
                "ok": False,
                "error": f"{label_b} falls within a mapped water or snow/ice body — pick a point on dry land.",
                "retry_worthy": False,
            }

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


def _check_walkable_connectivity(water_mask, start_rc: tuple[int, int],
                                   goal_rc: tuple[int, int]) -> bool:
    """
    Ignores cost entirely — floods outward from start_rc across any
    non-water cell and checks whether goal_rc is ever reached. Used only
    as a diagnostic once all padding-expansion attempts in
    route_any_two_points have failed: it distinguishes "these two points
    are genuinely cut off from each other by water within the search
    area" (most likely open ocean — a real geography limit, worth
    reporting plainly) from "something else is wrong" (land does connect
    them, so a bare 'no path found' would be misleading). Same technique
    validated earlier on the Lake Manapouri case, where it correctly
    distinguished a real geography limit from a bug.
    """
    from collections import deque

    if water_mask is None:
        return True  # no water data at all — connectivity isn't the question here

    n_rows, n_cols = water_mask.shape
    if water_mask[start_rc] or water_mask[goal_rc]:
        return False

    visited = np.zeros_like(water_mask, dtype=bool)
    visited[start_rc] = True
    queue = deque([start_rc])

    while queue:
        i, j = queue.popleft()
        if (i, j) == goal_rc:
            return True
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                ni, nj = i + di, j + dj
                if (0 <= ni < n_rows and 0 <= nj < n_cols
                        and not visited[ni, nj] and not water_mask[ni, nj]):
                    visited[ni, nj] = True
                    queue.append((ni, nj))

    return False


def _route_with_padding_retry(waypoints: list[tuple[float, float]],
                                attempt_fn,
                                node_budget: int = DEFAULT_NODE_BUDGET,
                                padding_frac: float = 0.3,
                                max_padding_attempts: int = 3,
                                padding_growth: float = 2.0,
                                max_attempt_seconds: float = 45.0) -> dict:
    """
    Shared retry-with-expanding-search-area core, used by BOTH
    route_any_two_points (2 waypoints) and route_via_waypoints (N
    waypoints via one shared grid) — extracted so this logic exists in
    exactly one place rather than being duplicated between them.

    attempt_fn(grid_state) -> dict is called once per attempt and must
    return the same {"ok": bool, ...} shape route_between_waypoints does.
    For a 2-point route that's just route_between_waypoints itself; for
    a multi-waypoint route it should attempt every leg using the given
    grid_state and return {"ok": False, "error": ..., "retry_worthy": ...}
    if even one leg fails, since a partial multi-waypoint route isn't
    usable. Propagate "retry_worthy" from whichever leg failed (see
    route_between_waypoints) so a water-blocked waypoint anywhere in the
    chain still short-circuits correctly, not just for the 2-point case.

    If no path is found, the search area is retried with progressively
    larger padding (multiplied by padding_growth each time, up to
    max_padding_attempts total tries) before giving up. This matters for
    routes that need a genuinely large detour — around a harbour,
    peninsula, or inlet — where the default padding is enough for normal
    inland ridge-avoidance but nowhere near enough to see the way around
    a large body of water. Each retry only happens for the routes that
    actually need it; a normal inland route still succeeds on the first,
    cheap, tightly-bounded attempt.

    RETRY-WORTHY SHORT-CIRCUIT: if attempt_fn returns
    "retry_worthy": False (a waypoint itself sits inside a mapped water
    or snow/ice body — see route_between_waypoints), expanding the
    search area can never fix that, since the point doesn't move. The
    loop stops immediately instead of burning further expensive attempts
    on something a bigger box was never going to solve.

    TIME-BASED CIRCUIT BREAKER: if a single attempt takes longer than
    max_attempt_seconds, the loop stops expanding rather than retrying
    with an even bigger, even more expensive search area. This was added
    after a real case — a long cross-country query whose bbox happened to
    pull in a large number of complex ocean coastline polygons — took
    120s on attempt 1, 370s on attempt 2, and crashed the machine on
    attempt 3. The exact cause of that scaling wasn't fully pinned down
    (candidates: polygon count/complexity in the water mask union,
    possibly interaction with how the split ocean chunks overlap at their
    seams — see prefetch_nz_water_data.py), but this breaker bounds the
    worst case regardless of the precise mechanism: total time is capped
    at roughly max_padding_attempts * max_attempt_seconds, not unbounded.

    Once all attempts are exhausted (by count, by hitting the time limit,
    or by the retry-worthy short-circuit), a connectivity check (ignoring
    cost, just "is there any walkable path at all", using the FIRST and
    LAST waypoints as the representative start/goal) determines whether
    the final error message reports a genuine geography limit (most
    likely open ocean) or a real bug worth investigating — skipped
    entirely for the retry-worthy=False case, since that failure is
    already specific and doesn't need a connectivity diagnosis on top.
    """
    current_padding = padding_frac
    last_grid_state = None

    for attempt in range(1, max_padding_attempts + 1):
        bbox = compute_dynamic_bbox_multi(waypoints, current_padding)
        cell_size_m = choose_cell_size_for_budget(bbox, node_budget)

        min_lat, min_lon, max_lat, max_lon = bbox
        mid_lat = (min_lat + max_lat) / 2
        m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)
        bbox_height_km = abs(max_lat - min_lat) * m_per_deg_lat / 1000
        bbox_width_km = abs(max_lon - min_lon) * m_per_deg_lon / 1000

        print(f"Attempt {attempt}/{max_padding_attempts} — padding={current_padding:.2f}, "
              f"bbox: {bbox_height_km:.1f}km x {bbox_width_km:.1f}km, "
              f"cell size: {cell_size_m:.1f}m")
        if cell_size_m > 100:
            print(f"NOTE: cell size {cell_size_m:.0f}m is too coarse to resolve real "
                  f"trail-scale terrain — treat this route as a rough estimate only.")

        t_attempt_start = time.time()
        grid_state = build_grid_state(bbox, cell_size_m)
        result = attempt_fn(grid_state)
        attempt_seconds = time.time() - t_attempt_start

        if result["ok"]:
            result["bbox_used"] = bbox
            result["cell_size_m_used"] = cell_size_m
            result["padding_frac_used"] = current_padding
            if attempt > 1:
                print(f"Route found on attempt {attempt} after expanding padding "
                      f"to {current_padding:.2f}.")
            return result

        last_grid_state = grid_state

        if not result.get("retry_worthy", True):
            print(f"Attempt {attempt}: {result['error']}")
            print("Not retrying with a bigger search area — the problem is a "
                  "waypoint itself, not the size of the search area.")
            return result

        if attempt_seconds > max_attempt_seconds:
            print(f"Attempt {attempt} took {attempt_seconds:.1f}s, over the "
                  f"{max_attempt_seconds:.0f}s circuit-breaker limit — stopping here "
                  f"rather than retrying with an even bigger, even more expensive "
                  f"search area.")
            break

        if attempt < max_padding_attempts:
            print(f"Attempt {attempt} found no path ({attempt_seconds:.1f}s) — "
                  f"expanding the search area and retrying.")
        current_padding *= padding_growth

    # All attempts exhausted (by count or by the time breaker) — diagnose
    # rather than return a bare failure.
    lats, lons = last_grid_state["lats"], last_grid_state["lons"]
    start_rc = find_nearest_rc(lats, lons, waypoints[0][0], waypoints[0][1])
    goal_rc = find_nearest_rc(lats, lons, waypoints[-1][0], waypoints[-1][1])
    connected = _check_walkable_connectivity(last_grid_state.get("water_mask"), start_rc, goal_rc)

    if connected:
        error_msg = (
            f"No path found (padding expanded up to {current_padding / padding_growth:.2f} "
            f"before stopping), but a connectivity check confirms land "
            f"DOES connect these points within the final search area — this points to a "
            f"real bug in the search or cost arrays, not a genuine geography limit."
        )
    else:
        error_msg = (
            f"No path found (padding expanded up to {current_padding / padding_growth:.2f} "
            f"before stopping). A connectivity check confirms these "
            f"points are genuinely separated by water within the final search area — most "
            f"likely open ocean, or a detour larger than this many rounds of padding "
            f"expansion can reach."
        )

    print(error_msg)
    return {"ok": False, "error": error_msg}


def route_any_two_points(waypoint_a: tuple[float, float],
                           waypoint_b: tuple[float, float],
                           node_budget: int = DEFAULT_NODE_BUDGET,
                           padding_frac: float = 0.3,
                           max_padding_attempts: int = 3,
                           padding_growth: float = 2.0,
                           max_attempt_seconds: float = 45.0) -> dict:
    """
    Performs a free search between two waypoints anywhere in New Zealand,
    building a bounding box and grid sized to the query. See
    _route_with_padding_retry for the retry/circuit-breaker/diagnostic
    behavior — this is a thin wrapper around it for the 2-point case.
    """
    def attempt_fn(grid_state):
        return route_between_waypoints(grid_state, waypoint_a, waypoint_b)

    return _route_with_padding_retry(
        [waypoint_a, waypoint_b], attempt_fn,
        node_budget=node_budget, padding_frac=padding_frac,
        max_padding_attempts=max_padding_attempts, padding_growth=padding_growth,
        max_attempt_seconds=max_attempt_seconds,
    )


def _waypoint_label(index: int, n_waypoints: int) -> str:
    """
    Names a waypoint by its position in a full multi-waypoint sequence,
    for use in route_via_waypoints' error messages. index 0 is always
    "Start point" and the last index is always "End point"; everything
    between is "Waypoint N" (matching the site's own terminology for the
    points placed between start and end, not "via point"), numbered to
    match how route_with_waypoints builds the full sequence
    (all_waypoints = [waypoint_a] + list(via) + [waypoint_b]) — so index
    1 is via[0] ("Waypoint 1"), index 2 is via[1] ("Waypoint 2"), and so
    on, matching the "via" list a caller actually sent (the "via" name
    here is this API's own internal field name, unrelated to the
    site's user-facing "waypoint" terminology).
    """
    if index == 0:
        return "Start point"
    if index == n_waypoints - 1:
        return "End point"
    return f"Waypoint {index}"


def route_via_waypoints(waypoints: list[tuple[float, float]],
                          node_budget: int = DEFAULT_NODE_BUDGET,
                          padding_frac: float = 0.3,
                          max_padding_attempts: int = 3,
                          padding_growth: float = 2.0,
                          max_attempt_seconds: float = 45.0) -> dict:
    """
    Routes through an ordered sequence of waypoints — for example, scenic
    stops placed by the user — using a single grid built to cover the
    entire chain. Each leg between consecutive waypoints is searched
    independently for its own lowest-cost path. Now shares the same
    retry-with-expanding-padding, circuit-breaker, and connectivity-
    diagnosis behavior route_any_two_points already had — previously this
    function had none of that, so a multi-waypoint route needing a
    genuinely large detour (the same harbour/peninsula case that
    motivated the retry logic in the first place) would fail immediately
    with a bare "No path found" instead of getting a chance to expand the
    search area. See _route_with_padding_retry for the full behavior.
    """
    if len(waypoints) < 2:
        raise ValueError("Need at least 2 waypoints")

    n_waypoints = len(waypoints)

    def attempt_fn(grid_state):
        total_distance_km = 0.0
        total_hours = 0.0
        total_climb_m = 0.0
        full_coords = []
        full_elevations = []
        for leg_num, (a, b) in enumerate(zip(waypoints[:-1], waypoints[1:]), start=1):
            label_a = _waypoint_label(leg_num - 1, n_waypoints)
            label_b = _waypoint_label(leg_num, n_waypoints)
            leg = route_between_waypoints(grid_state, a, b, label_a=label_a, label_b=label_b)
            if not leg["ok"]:
                return {
                    "ok": False,
                    "error": f"Leg {leg_num} ({a} -> {b}) failed: {leg['error']}",
                    # Propagated so a water-blocked waypoint anywhere in the
                    # chain short-circuits the retry loop correctly, same as
                    # the 2-point case.
                    "retry_worthy": leg.get("retry_worthy", True),
                }
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
        }

    print(f"Multi-waypoint routing for {len(waypoints)} waypoints")
    return _route_with_padding_retry(
        waypoints, attempt_fn,
        node_budget=node_budget, padding_frac=padding_frac,
        max_padding_attempts=max_padding_attempts, padding_growth=padding_growth,
        max_attempt_seconds=max_attempt_seconds,
    )


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


# ── Security: never let an API key reach a user-facing error message ────
#
# A real incident: a failed DEM tile request's HTTPError (whose default
# string form includes the full request URL, LINZ API key included as a
# plain query parameter) propagated unmodified through build_grid_state
# and out through lambda_handler's `f"Routing failed: {e}"`, putting the
# raw key in front of the end user. fetch_dem_mosaic's fetch_one() now
# sanitizes this at its source (the actual fix), but this function is a
# second, independent layer: it scans ANY error string for an api=...
# query parameter — by pattern, not by comparing against today's known
# key value — and redacts it, so a future exception path nobody thought
# to sanitize at the source still can't leak a key through this same
# route. Applied at both API response boundaries (Lambda and Flask)
# below, right before an error ever leaves the process.

def redact_api_keys(text: str) -> str:
    """Redacts any 'api=<value>' query parameter found anywhere in text."""
    return re.sub(r'([?&]api=)[^&\s"\']+', r'\1***REDACTED***', text)


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

        if body.get("warm"):
            try:
                _warm_up()
            except Exception as e:
                print(f"WARNING: warm-up failed ({e}) — the next real request "
                      f"will pay the cold-start cost itself instead.")
            return jsonify({"ok": True, "warm": True})

        a = tuple(body["a"])
        b = tuple(body["b"])
        via = [tuple(p) for p in body.get("via", [])] or None
        try:
            result = route_with_waypoints(a, b, via=via, node_budget=node_budget)
            if result.get("ok"):
                result["gpx"] = route_to_gpx(result)
            elif "error" in result:
                result["error"] = redact_api_keys(result["error"])
        except Exception as e:
            result = {"ok": False, "error": redact_api_keys(f"Routing failed: {e}")}
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


def _warm_up():
    """
    Performs the expensive one-time cold-start work ahead of a real route
    request, so it's already done by the time the user actually clicks
    Generate Route: loads the local water/stream/bridge dataset (62,066
    water polygons, 94,835 stream lines, 23,028 bridge crossings — see
    prefetch_nz_water_data.py — building all three STRtree spatial
    indexes), and runs a tiny synthetic A* search purely to trigger
    Numba's JIT compilation of the core search functions ahead of time.
    Both are real, measured costs from earlier in this project — this
    doesn't remove either cost, it just relocates WHEN it's paid: from
    "the moment a user clicks Generate and is watching a spinner" to
    "whenever the frontend fires a warm-up ping after the page loads",
    which is invisible to them either way.

    Touches nothing LINZ/DEM-related — no real coordinates are involved
    here, so there's no reason to burn a network call for tiles nobody
    has asked about yet.

    Safe to call more than once: _load_local_water_index() already
    caches itself (see its own module-level guard), and Numba's
    cache=True means a second JIT "compilation" this session is just a
    cache lookup, not a repeat of the real work.
    """
    _load_local_water_index()

    # A minimal synthetic grid, just large enough to exercise every
    # Numba-jitted code path (all 16 directions, the heap push/pop, the
    # heuristic) — the actual result is thrown away; only the
    # compilation side effect matters.
    tiny_lats = np.array([-41.0, -41.0001, -41.0002])
    tiny_lons = np.array([174.0, 174.0001, 174.0002])
    tiny_elevation = np.zeros((3, 3))
    tiny_costs = compute_directional_costs(
        tiny_elevation, tiny_lats,
        abs(tiny_lats[1] - tiny_lats[0]), abs(tiny_lons[1] - tiny_lons[0]),
    )
    astar_on_grid(tiny_costs, tiny_lats, tiny_lons, (0, 0), (2, 2))


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
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        return {
            "statusCode": 400,
            "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": redact_api_keys(f"Bad request: {e}")}),
        }

    # Lambda warming — see _warm_up()'s docstring. Checked before requiring
    # "a"/"b" to be present, since a warm-up ping deliberately carries
    # neither.
    if payload.get("warm"):
        try:
            _warm_up()
        except Exception as e:
            # A failed warm-up should never surface as an error the
            # frontend has to handle — worst case, the next real request
            # just pays the cold-start cost itself, exactly as it would
            # have without this feature at all.
            print(f"WARNING: warm-up failed ({e}) — the next real request "
                  f"will pay the cold-start cost itself instead.")
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True, "warm": True})}

    try:
        waypoint_a = tuple(payload["a"])
        waypoint_b = tuple(payload["b"])
        via = [tuple(p) for p in payload.get("via", [])] or None
        node_budget = int(payload.get("node_budget", DEFAULT_NODE_BUDGET))
    except (KeyError, TypeError, ValueError) as e:
        return {
            "statusCode": 400,
            "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": redact_api_keys(f"Bad request: {e}")}),
        }

    try:
        result = route_with_waypoints(waypoint_a, waypoint_b, via=via, node_budget=node_budget)
        if result.get("ok"):
            # Hand the frontend the exact, already-tested GPX output rather
            # than making it reconstruct one from raw coordinates — same
            # route_to_gpx() the CLI's --out uses, just returned in the API
            # response instead of written to a local file.
            result["gpx"] = route_to_gpx(result)
        elif "error" in result:
            # Defense in depth: also redact any error message returned
            # normally (not via an exception) — cheap insurance in case a
            # future code path builds an error string containing a URL.
            result["error"] = redact_api_keys(result["error"])
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": redact_api_keys(f"Routing failed: {e}")}),
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

handler = lambda_handler
