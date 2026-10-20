"""Terrain-cost hiking route generator for New Zealand, served as an AWS Lambda function and a CLI."""

import base64
import json
import math
import os
import pickle
import re
import time
import traceback
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import numpy as np
import requests
import shapely
from numba import njit
from PIL import Image
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.strtree import STRtree

# ── Configuration ────────────────────────────────────────────────────────

ELEVATION_TILE_URL = (
    "https://basemaps.linz.govt.nz/v1/tiles/elevation/WebMercatorQuad/"
    "{z}/{x}/{y}.png?api={key}&pipeline=terrain-rgb"
)
TILE_SIZE = 256
NZ_WATER_DATA_FILE = Path(os.environ.get("NZ_WATER_DATA_FILE", "nz_water_data.pkl"))

TOBLER_STEEPNESS_FACTOR = 4.0   # Higher values penalise steep terrain more heavily.
TOBLER_DOWNHILL_BIAS = 0.05     # Tobler's constant: a slight downhill grade is fastest.
RIDGE_CROSS_SLOPE_WEIGHT = 2.0  # Penalty for sidling across an open slope (0 disables it).
STREAM_PROXIMITY_WEIGHT = 1.5   # Cost multiplier for steps near a mapped stream (1.0 disables it).
STREAM_BUFFER_M = 15.0          # Distance from a stream centreline that counts as near the stream.
BRIDGE_BUFFER_M = 8.0           # Distance from a bridge centreline that counts as on the bridge.
BRIDGE_CENTERING_PENALTY = 0.4  # Extra cost at the edge of a bridge corridor, pulling routes onto its centreline.
CROSSING_SAMPLES = 3            # Points sampled along each step when checking for water or bridge crossings.

# The 8 compass directions plus 8 knight-move directions, so paths can bend more gradually than 45 degrees.
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
    """Returns the LINZ API key from the environment or AWS Secrets Manager, cached after the first lookup."""
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


# ── Geometry and coordinate helpers ──────────────────────────────────────

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Returns the great-circle distance in kilometres between two (lon, lat) points."""
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
    """Returns (metres per degree of latitude, metres per degree of longitude) at the given latitude."""
    return 111_320.0, 111_320.0 * math.cos(math.radians(lat))


def zoom_for_resolution(cell_size_m: float, lat: float,
                          min_zoom: int = 10, max_zoom: int = 15) -> int:
    """Returns the smallest Web Mercator zoom level whose pixel size is no larger than cell_size_m."""
    for z in range(min_zoom, max_zoom + 1):
        meters_per_pixel = 156_543.03392 * math.cos(math.radians(lat)) / (2 ** z)
        if meters_per_pixel <= cell_size_m:
            return z
    return max_zoom


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Returns the Web Mercator tile (x, y) that contains a (lon, lat) point."""
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    x_float = (lon + 180.0) / 360.0 * n
    y_float = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return int(x_float), int(y_float)


def decode_terrain_rgb(img: Image.Image) -> np.ndarray:
    """Decodes a terrain-RGB tile image into elevations in metres."""
    arr = np.asarray(img.convert("RGB"), dtype=np.float64)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    return -10000 + (r * 256 * 256 + g * 256 + b) * 0.1


def tobler_speed_kmh_array(slope: np.ndarray) -> np.ndarray:
    """Returns Tobler's walking speed in km/h for an array of slopes."""
    return 6.0 * np.exp(-TOBLER_STEEPNESS_FACTOR * np.abs(slope + TOBLER_DOWNHILL_BIAS))


# ── Step 1: Elevation data ───────────────────────────────────────────────

def fetch_dem_mosaic(bbox: tuple[float, float, float, float], zoom: int):
    """Downloads the LINZ elevation tiles covering bbox in parallel and stitches them into one array."""
    api_key = get_linz_key()

    min_lat, min_lon, max_lat, max_lon = bbox
    x0, y0 = lonlat_to_tile(min_lon, max_lat, zoom)
    x1, y1 = lonlat_to_tile(max_lon, min_lat, zoom)

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
            # Re-raise without the API key in the URL, and without chaining the original error.
            safe_url = url.replace(api_key, "***REDACTED***")
            raise RuntimeError(
                f"DEM tile fetch failed ({type(e).__name__}) for {safe_url}"
            ) from None
        elev = decode_terrain_rgb(Image.open(BytesIO(resp.content)))
        return ti, tj, elev

    with ThreadPoolExecutor(max_workers=10) as executor:
        for ti, tj, elev in executor.map(fetch_one, tasks):
            mosaic[ti * TILE_SIZE:(ti + 1) * TILE_SIZE,
                   tj * TILE_SIZE:(tj + 1) * TILE_SIZE] = elev

    print(f"Fetched {len(tasks)} DEM tiles, mosaic shape {mosaic.shape}.")
    return mosaic, x0, y0


def build_elevation_grid(bbox: tuple[float, float, float, float],
                           cell_size_m: float,
                           mosaic: np.ndarray, origin_x: int, origin_y: int,
                           zoom: int):
    """Resamples the DEM mosaic onto a regular lat/lon grid with the given cell size in metres."""
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


# ── Step 2: Water, stream and bridge data (pre-built by prefetch_nz_water_data.py) ──

_local_water_index = None


def _load_local_water_index():
    """Loads the pre-built water, stream and bridge dataset once per process and indexes it spatially."""
    global _local_water_index
    if _local_water_index is not None:
        return _local_water_index

    if not NZ_WATER_DATA_FILE.exists():
        raise FileNotFoundError(
            f"Water dataset {NZ_WATER_DATA_FILE} not found — run prefetch_nz_water_data.py to build it."
        )

    with open(NZ_WATER_DATA_FILE, "rb") as f:
        (water_polygons, stream_lines, global_water_union, global_stream_buffer_union,
         bridge_lines, _) = pickle.load(f)

    print(f"Loaded local water dataset: {len(water_polygons)} water polygons, "
          f"{len(stream_lines)} stream lines, {len(bridge_lines)} bridge crossings.")

    _local_water_index = {
        "water_tree": STRtree(water_polygons), "water_polygons": water_polygons,
        "stream_tree": STRtree(stream_lines), "stream_lines": stream_lines,
        "bridge_tree": STRtree(bridge_lines), "bridge_lines": bridge_lines,
        "global_water_union": global_water_union,
        "global_stream_buffer_union": global_stream_buffer_union,
    }
    return _local_water_index


def _features_in_area(tree: STRtree, geometries: list, area) -> list:
    """Returns the indexed geometries that intersect the given area."""
    return [geometries[i] for i in tree.query(area) if geometries[i].intersects(area)]


def rasterize_precomputed_geometry(merged_geometry, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Returns a boolean grid marking which cell centres fall inside an already-merged geometry."""
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return shapely.contains_xy(merged_geometry, lon_grid, lat_grid)


def _compute_candidate_crossing_region(geometry, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Returns the cells inside the geometry's bounding box (padded by two cells) that need a crossing check."""
    n_rows, n_cols = len(lats), len(lons)
    lat_step = abs(lats[1] - lats[0]) if n_rows > 1 else 0.0
    lon_step = abs(lons[1] - lons[0]) if n_cols > 1 else 0.0
    pad_lat = 2.0 * lat_step
    pad_lon = 2.0 * lon_step

    min_x, min_y, max_x, max_y = geometry.bounds
    min_x -= pad_lon
    max_x += pad_lon
    min_y -= pad_lat
    max_y += pad_lat

    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return (lon_grid >= min_x) & (lon_grid <= max_x) & (lat_grid >= min_y) & (lat_grid <= max_y)


def _compute_step_crossing_blocked(geometry, lats: np.ndarray, lons: np.ndarray,
                                     di: int, dj: int, candidate_region: np.ndarray) -> np.ndarray:
    """Marks steps in direction (di, dj) whose sampled in-between points fall inside the geometry."""
    n_rows, n_cols = len(lats), len(lons)
    if not candidate_region.any():
        return np.zeros((n_rows, n_cols), dtype=bool)

    lat_step = lats[1] - lats[0] if n_rows > 1 else 0.0
    lon_step = lons[1] - lons[0] if n_cols > 1 else 0.0

    row_idx = np.arange(n_rows)
    col_idx = np.arange(n_cols)

    blocked = np.zeros((n_rows, n_cols), dtype=bool)
    for k in range(1, CROSSING_SAMPLES + 1):
        t = k / (CROSSING_SAMPLES + 1)
        sample_row = row_idx + di * t
        sample_col = col_idx + dj * t

        sample_lat_1d = lats[0] + sample_row * lat_step
        sample_lon_1d = lons[0] + sample_col * lon_step
        sample_lat_grid = np.broadcast_to(sample_lat_1d[:, None], (n_rows, n_cols))
        sample_lon_grid = np.broadcast_to(sample_lon_1d[None, :], (n_rows, n_cols))

        hits = np.zeros((n_rows, n_cols), dtype=bool)
        hits[candidate_region] = shapely.contains_xy(
            geometry, sample_lon_grid[candidate_region], sample_lat_grid[candidate_region]
        )
        blocked |= hits

    return blocked


def apply_water_mask(cost_arrays: dict, water_mask: np.ndarray, lats: np.ndarray, lons: np.ndarray,
                       local_water_geometry) -> None:
    """Hard-blocks every step that touches or passes over water by setting its cost to infinity."""
    water_float = water_mask.astype(np.float64)
    # No local water means nothing within reach of the grid, so the crossing check is skipped.
    candidate_region = None
    if local_water_geometry is not None:
        candidate_region = _compute_candidate_crossing_region(local_water_geometry, lats, lons)

    for di, dj in DIRECTIONS:
        neighbor_is_water = shifted(water_float, di, dj)
        neighbor_is_water = np.nan_to_num(neighbor_is_water, nan=0.0) >= 0.5
        blocked = water_mask | neighbor_is_water

        if candidate_region is not None:
            blocked = blocked | _compute_step_crossing_blocked(
                local_water_geometry, lats, lons, di, dj, candidate_region
            )

        cost_arrays[(di, dj)] = np.where(blocked, np.inf, cost_arrays[(di, dj)])


def apply_stream_proximity_penalty(cost_arrays: dict, stream_mask: np.ndarray, lats: np.ndarray,
                                     lons: np.ndarray, local_stream_geometry) -> None:
    """Multiplies the cost of every step that touches or passes through a stream corridor."""
    if STREAM_PROXIMITY_WEIGHT == 1.0:
        return

    stream_float = stream_mask.astype(np.float64)
    # No local streams means nothing within reach of the grid, so the crossing check is skipped.
    candidate_region = None
    if local_stream_geometry is not None:
        candidate_region = _compute_candidate_crossing_region(local_stream_geometry, lats, lons)

    for di, dj in DIRECTIONS:
        neighbor_in_zone = shifted(stream_float, di, dj)
        neighbor_in_zone = np.nan_to_num(neighbor_in_zone, nan=0.0) >= 0.5
        in_zone = stream_mask | neighbor_in_zone

        if candidate_region is not None:
            in_zone = in_zone | _compute_step_crossing_blocked(
                local_stream_geometry, lats, lons, di, dj, candidate_region
            )

        cost_arrays[(di, dj)] = np.where(
            in_zone, cost_arrays[(di, dj)] * STREAM_PROXIMITY_WEIGHT, cost_arrays[(di, dj)]
        )


def _bridge_line_segments(local_bridge_lines: list) -> list:
    """Splits clipped bridge lines into (ax, ay, bx, by) segments, skipping zero-length ones."""
    segments = []
    for geom in local_bridge_lines:
        if geom is None or geom.is_empty:
            continue
        lines = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for line in lines:
            if line.is_empty or line.geom_type != "LineString":
                continue
            coords = list(line.coords)
            for (ax, ay), (bx, by) in zip(coords[:-1], coords[1:]):
                if (ax, ay) != (bx, by):
                    segments.append((ax, ay, bx, by))
    return segments


def apply_bridge_crossings(cost_arrays: dict, original_cost_arrays: dict,
                             lats: np.ndarray, lons: np.ndarray, local_bridge_lines: list) -> None:
    """Restores the unmasked cost for steps that cross a mapped bridge, with a small pull towards its centreline."""
    segments = _bridge_line_segments(local_bridge_lines)
    if not segments:
        return

    n_rows, n_cols = len(lats), len(lons)
    lat_step = lats[1] - lats[0] if n_rows > 1 else 0.0
    lon_step = lons[1] - lons[0] if n_cols > 1 else 0.0
    mid_lat = float(np.mean(lats))
    m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)

    best_dist_m = {(di, dj): np.full((n_rows, n_cols), np.inf) for di, dj in DIRECTIONS}

    buffer_deg_lat = BRIDGE_BUFFER_M / m_per_deg_lat
    buffer_deg_lon = BRIDGE_BUFFER_M / m_per_deg_lon

    for ax, ay, bx, by in segments:
        # Limit the work to a padded sub-grid around this segment, since bridges are tiny relative to the grid.
        min_lon_seg, max_lon_seg = min(ax, bx), max(ax, bx)
        min_lat_seg, max_lat_seg = min(ay, by), max(ay, by)
        pad_lon = buffer_deg_lon * 1.5 + abs(lon_step) * 2
        pad_lat = buffer_deg_lat * 1.5 + abs(lat_step) * 2

        col_lo = int(np.floor((min_lon_seg - pad_lon - lons[0]) / lon_step)) if lon_step else 0
        col_hi = int(np.ceil((max_lon_seg + pad_lon - lons[0]) / lon_step)) + 1 if lon_step else n_cols
        row_lo = int(np.floor((min_lat_seg - pad_lat - lats[0]) / lat_step)) if lat_step else 0
        row_hi = int(np.ceil((max_lat_seg + pad_lat - lats[0]) / lat_step)) + 1 if lat_step else n_rows

        row_lo = max(0, row_lo)
        row_hi = min(n_rows, row_hi)
        col_lo = max(0, col_lo)
        col_hi = min(n_cols, col_hi)

        if row_lo >= row_hi or col_lo >= col_hi:
            continue

        sub_rows = np.arange(row_lo, row_hi)
        sub_cols = np.arange(col_lo, col_hi)
        sub_shape = (len(sub_rows), len(sub_cols))
        sub_slice = np.s_[row_lo:row_hi, col_lo:col_hi]

        ax_m, ay_m = ax * m_per_deg_lon, ay * m_per_deg_lat
        bx_m, by_m = bx * m_per_deg_lon, by * m_per_deg_lat
        abx, aby = bx_m - ax_m, by_m - ay_m
        ab_dot_ab = abx * abx + aby * aby
        if ab_dot_ab == 0:
            continue

        for di, dj in DIRECTIONS:
            for k in range(1, CROSSING_SAMPLES + 1):
                t = k / (CROSSING_SAMPLES + 1)
                sample_row = sub_rows + di * t
                sample_col = sub_cols + dj * t
                sample_lat_1d = lats[0] + sample_row * lat_step
                sample_lon_1d = lons[0] + sample_col * lon_step
                sample_x_m = np.broadcast_to((sample_lon_1d * m_per_deg_lon)[None, :], sub_shape)
                sample_y_m = np.broadcast_to((sample_lat_1d * m_per_deg_lat)[:, None], sub_shape)

                # Project onto the segment; points whose closest approach lies beyond either end don't count.
                apx = sample_x_m - ax_m
                apy = sample_y_m - ay_m
                t_raw = (apx * abx + apy * aby) / ab_dot_ab
                valid = (t_raw >= 0.0) & (t_raw <= 1.0)

                t_clamped = np.clip(t_raw, 0.0, 1.0)
                closest_x = ax_m + t_clamped * abx
                closest_y = ay_m + t_clamped * aby
                dist_m = np.sqrt((sample_x_m - closest_x) ** 2 + (sample_y_m - closest_y) ** 2)
                dist_m = np.where(valid, dist_m, np.inf)

                current = best_dist_m[(di, dj)][sub_slice]
                best_dist_m[(di, dj)][sub_slice] = np.minimum(current, dist_m)

    for di, dj in DIRECTIONS:
        d = best_dist_m[(di, dj)]
        within_buffer = d <= BRIDGE_BUFFER_M
        multiplier = 1.0 + BRIDGE_CENTERING_PENALTY * np.clip(d / BRIDGE_BUFFER_M, 0.0, 1.0)
        restored = original_cost_arrays[(di, dj)] * multiplier
        cost_arrays[(di, dj)] = np.where(within_buffer, restored, cost_arrays[(di, dj)])


# ── Step 3: Directional movement costs ───────────────────────────────────

def shifted(array: np.ndarray, di: int, dj: int) -> np.ndarray:
    """Returns the array shifted so result[i, j] = array[i + di, j + dj], with NaN outside the grid."""
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
    """Returns walking time in hours for every cell and direction, using Tobler's function plus a sidle penalty."""
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

    costs = {}
    for di, dj in DIRECTIONS:
        neighbor_elev = shifted(elevation_grid, di, dj)
        rise = neighbor_elev - elevation_grid
        dist_m = real_dist_m(di, dj)

        # Elevation either side of the step, used to penalise cutting across a slope.
        pdi, pdj = -dj, di
        elev_plus = shifted(elevation_grid, pdi, pdj)
        elev_minus = shifted(elevation_grid, -pdi, -pdj)
        cross_rise = elev_plus - elev_minus
        cross_dist_m = real_dist_m(pdi, pdj) * 2.0

        with np.errstate(invalid="ignore", divide="ignore"):
            slope = rise / dist_m
            speed_kmh = tobler_speed_kmh_array(slope)
            base_cost_hours = (dist_m / 1000.0) / speed_kmh

            cross_slope = np.nan_to_num(cross_rise / cross_dist_m, nan=0.0)
            sidle_penalty = 1.0 + RIDGE_CROSS_SLOPE_WEIGHT * np.abs(cross_slope)

            cost_hours = base_cost_hours * sidle_penalty

        costs[(di, dj)] = cost_hours

    return costs


# ── Step 4: A* search (compiled with Numba) ──────────────────────────────

TOBLER_MAX_SPEED_KMH = 6.0 * math.exp(TOBLER_STEEPNESS_FACTOR * TOBLER_DOWNHILL_BIAS)


@njit(cache=True)
def _heap_push(heap_f, heap_i, heap_j, size, f, i, j):
    """Pushes a node onto the binary min-heap and returns the new heap size."""
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
    """Pops the lowest-cost node from the binary min-heap."""
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
    """Estimates the remaining walking time as straight-line distance at the maximum possible speed."""
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
    """Runs A* over the cost grid and returns predecessor arrays plus whether the goal was reached."""
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
    """Returns the lowest-cost path as a list of (row, col) cells, or None if the goal is unreachable."""
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


# ── Grid assembly and single-leg routing ─────────────────────────────────

def build_grid_state(bbox: tuple[float, float, float, float], cell_size_m: float) -> dict:
    """Builds the elevation grid and the directional cost arrays, with water, stream and bridge rules applied."""
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

    # Lookup area a few cells wider than the grid, so nearby features count and geometry work stays small.
    clip_pad = max(lat_step, lon_step) * 5
    clip_box = box(min_lon - clip_pad, min_lat - clip_pad, max_lon + clip_pad, max_lat + clip_pad)

    # Rasterise water and streams onto the grid and build small local geometries for the crossing checks.
    try:
        index = _load_local_water_index()
        water_polygons = _features_in_area(index["water_tree"], index["water_polygons"], clip_box)
        stream_lines = _features_in_area(index["stream_tree"], index["stream_lines"], clip_box)
        bridge_lines = _features_in_area(index["bridge_tree"], index["bridge_lines"], clip_box)

        water_mask = rasterize_precomputed_geometry(index["global_water_union"], lats, lons)
        stream_mask = rasterize_precomputed_geometry(index["global_stream_buffer_union"], lats, lons)

        clipped_water = [p.intersection(clip_box) for p in water_polygons]
        clipped_water = [p for p in clipped_water if not p.is_empty]
        clipped_streams = [l.intersection(clip_box) for l in stream_lines]
        clipped_streams = [l for l in clipped_streams if not l.is_empty]
        local_water_union = unary_union(clipped_water) if clipped_water else None

        local_stream_union = None
        if clipped_streams:
            m_per_deg_lat, m_per_deg_lon = meters_per_degree(mid_lat)
            stream_buffer_deg = STREAM_BUFFER_M / min(m_per_deg_lat, m_per_deg_lon)
            local_stream_union = unary_union(clipped_streams).buffer(stream_buffer_deg)

        n_water_features = len(water_polygons)
        n_stream_features = len(stream_lines)
    except Exception as e:
        water_mask = np.zeros((n_rows, n_cols), dtype=bool)
        stream_mask = np.zeros((n_rows, n_cols), dtype=bool)
        local_water_union = None
        local_stream_union = None
        bridge_lines = []
        n_water_features = 0
        n_stream_features = 0
        print(f"WARNING: water/stream lookup failed ({e}) — routing without water "
              f"blocking or stream penalty.")
        traceback.print_exc()
    t2b = time.time()

    cost_arrays = compute_directional_costs(elevation_grid, lats, lat_step, lon_step)

    # Snapshot taken before masking, so bridge crossings can restore their normal terrain cost.
    original_cost_arrays = {k: v.copy() for k, v in cost_arrays.items()}

    apply_stream_proximity_penalty(cost_arrays, stream_mask, lats, lons, local_stream_union)
    apply_water_mask(cost_arrays, water_mask, lats, lons, local_water_union)

    n_bridge_features = len(bridge_lines)
    if bridge_lines:
        clipped_bridges = [l.intersection(clip_box) for l in bridge_lines]
        clipped_bridges = [l for l in clipped_bridges if not l.is_empty]
        apply_bridge_crossings(cost_arrays, original_cost_arrays, lats, lons, clipped_bridges)
    t3 = time.time()

    print(f"Grid build timing — fetch: {t1 - t0:.2f}s, sample: {t2 - t1:.3f}s, "
          f"water/stream: {t2b - t2:.2f}s ({n_water_features} water features, "
          f"{int(water_mask.sum())} cells blocked; {n_stream_features} stream features, "
          f"{int(stream_mask.sum())} cells in proximity zone; {n_bridge_features} bridge crossings, "
          f"of {n_rows * n_cols} total), cost arrays: {t3 - t2b:.3f}s, total: {t3 - t0:.2f}s")

    return {
        "elevation_grid": elevation_grid,
        "lats": lats,
        "lons": lons,
        "cost_arrays": cost_arrays,
        "water_mask": water_mask,
    }


def find_nearest_rc(lats: np.ndarray, lons: np.ndarray,
                      lon: float, lat: float) -> tuple[int, int]:
    """Snaps a (lon, lat) coordinate to the nearest grid row and column."""
    i = int(np.argmin(np.abs(lats - lat)))
    j = int(np.argmin(np.abs(lons - lon)))
    return i, j


def route_between_waypoints(grid_state: dict,
                              waypoint_a: tuple[float, float],
                              waypoint_b: tuple[float, float],
                              label_a: str = "Start point",
                              label_b: str = "End point") -> dict:
    """Finds the route between two waypoints on an existing grid and returns its geometry and statistics."""
    lats, lons = grid_state["lats"], grid_state["lons"]
    cost_arrays = grid_state["cost_arrays"]
    elevation_grid = grid_state["elevation_grid"]
    water_mask = grid_state["water_mask"]

    start_rc = find_nearest_rc(lats, lons, waypoint_a[0], waypoint_a[1])
    goal_rc = find_nearest_rc(lats, lons, waypoint_b[0], waypoint_b[1])

    # A waypoint that snaps onto water can't be fixed by a bigger search area, so the retry loop is told to stop.
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


# ── Search area sizing, retries and the main routing entry point ─────────

DEFAULT_NODE_BUDGET = 2_000_000  # Maximum grid nodes per search; the cell size grows to stay within it.
MIN_CELL_SIZE_M = 10.0
MAX_CELL_SIZE_M = 1_000.0
MIN_PADDING_DEG = 0.01


def compute_dynamic_bbox_multi(waypoints: list[tuple[float, float]],
                                 padding_frac: float = 0.3) -> tuple[float, float, float, float]:
    """Builds a padded bounding box around the waypoints so the route has room to detour."""
    lons = [w[0] for w in waypoints]
    lats = [w[1] for w in waypoints]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)

    lon_pad = max((max_lon - min_lon) * padding_frac, MIN_PADDING_DEG)
    lat_pad = max((max_lat - min_lat) * padding_frac, MIN_PADDING_DEG)

    return (min_lat - lat_pad, min_lon - lon_pad, max_lat + lat_pad, max_lon + lon_pad)


def choose_cell_size_for_budget(bbox: tuple[float, float, float, float],
                                  node_budget: int = DEFAULT_NODE_BUDGET) -> float:
    """Chooses a cell size that keeps the grid within node_budget nodes, clamped to a sensible range."""
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


def _check_walkable_connectivity(water_mask: np.ndarray, start_rc: tuple[int, int],
                                   goal_rc: tuple[int, int]) -> bool:
    """Flood-fills dry land from start_rc and reports whether goal_rc can be reached at all."""
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
    """Retries a routing attempt with a progressively larger search area, then explains the failure if none works."""
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
            print(f"NOTE: cell size {cell_size_m:.0f}m is too coarse for trail-scale terrain — "
                  f"treat this route as a rough estimate.")

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
            print("Not retrying — the problem is a waypoint, not the size of the search area.")
            return result

        if attempt_seconds > max_attempt_seconds:
            print(f"Attempt {attempt} took {attempt_seconds:.1f}s, over the "
                  f"{max_attempt_seconds:.0f}s limit — not expanding the search area further.")
            break

        if attempt < max_padding_attempts:
            print(f"Attempt {attempt} found no path ({attempt_seconds:.1f}s) — "
                  f"expanding the search area and retrying.")
        current_padding *= padding_growth

    # Every attempt failed, so check whether dry land connects the end points at all.
    lats, lons = last_grid_state["lats"], last_grid_state["lons"]
    start_rc = find_nearest_rc(lats, lons, waypoints[0][0], waypoints[0][1])
    goal_rc = find_nearest_rc(lats, lons, waypoints[-1][0], waypoints[-1][1])
    connected = _check_walkable_connectivity(last_grid_state["water_mask"], start_rc, goal_rc)

    if connected:
        error_msg = "No path found - but paths are connected"
    else:
        error_msg = "No path found - most likely due to open ocean, or a detour larger than we can reach"

    print(error_msg)
    return {"ok": False, "error": error_msg}


def route_any_two_points(waypoint_a: tuple[float, float],
                           waypoint_b: tuple[float, float],
                           node_budget: int = DEFAULT_NODE_BUDGET) -> dict:
    """Routes between two points anywhere in New Zealand with automatic search-area retries."""
    def attempt_fn(grid_state):
        return route_between_waypoints(grid_state, waypoint_a, waypoint_b)

    return _route_with_padding_retry([waypoint_a, waypoint_b], attempt_fn, node_budget=node_budget)


def _waypoint_label(index: int, n_waypoints: int) -> str:
    """Names a point by its position in the route, matching the site's Start point / Waypoint N / End point labels."""
    if index == 0:
        return "Start point"
    if index == n_waypoints - 1:
        return "End point"
    return f"Waypoint {index}"


def _find_water_blocked_waypoint(waypoints: list[tuple[float, float]]) -> dict | None:
    """Rejects any waypoint inside mapped water before a grid is built, returning the error or None."""
    global_water = _load_local_water_index()["global_water_union"]
    n_waypoints = len(waypoints)
    for i, (lon, lat) in enumerate(waypoints):
        if shapely.contains_xy(global_water, lon, lat):
            label = _waypoint_label(i, n_waypoints)
            return {
                "ok": False,
                "error": f"{label} falls within a mapped water or snow/ice body — pick a point on dry land.",
                "retry_worthy": False,
            }
    return None


def route_via_waypoints(waypoints: list[tuple[float, float]],
                          node_budget: int = DEFAULT_NODE_BUDGET) -> dict:
    """Routes through an ordered list of waypoints on one shared grid, leg by leg, with search-area retries."""
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
                # This "Leg N (...) failed:" format is parsed by the frontend's friendlyRouteError().
                return {
                    "ok": False,
                    "error": f"Leg {leg_num} ({a} -> {b}) failed: {leg['error']}",
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

    print(f"Multi-waypoint routing for {n_waypoints} waypoints")
    return _route_with_padding_retry(waypoints, attempt_fn, node_budget=node_budget)


def route_with_waypoints(waypoint_a: tuple[float, float],
                           waypoint_b: tuple[float, float],
                           via: list[tuple[float, float]] | None = None,
                           node_budget: int = DEFAULT_NODE_BUDGET) -> dict:
    """Main entry point: checks the waypoints, then routes A to B directly or through the via points in order."""
    all_waypoints = [waypoint_a] + list(via or []) + [waypoint_b]

    early_check = _find_water_blocked_waypoint(all_waypoints)
    if early_check is not None:
        return early_check

    if not via:
        return route_any_two_points(waypoint_a, waypoint_b, node_budget=node_budget)

    return route_via_waypoints(all_waypoints, node_budget=node_budget)


# ── GPX export ───────────────────────────────────────────────────────────

def route_to_gpx(result: dict, track_name: str = "Ridge Walker Route") -> str:
    """Converts a successful routing result into a GPX 1.1 track with per-point elevation."""
    if not result.get("ok"):
        raise ValueError("Cannot export a failed routing result to GPX.")

    coordinates = result["route"]["geometry"]["coordinates"]
    elevations = result["elevations_m"]

    gpx = ET.Element("gpx", attrib={
        "version": "1.1",
        "creator": "RidgeWalker",
        "xmlns": "http://www.topografix.com/GPX/1/1",
    })
    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = track_name
    trkseg = ET.SubElement(trk, "trkseg")

    for (lon, lat), ele in zip(coordinates, elevations):
        trkpt = ET.SubElement(trkseg, "trkpt", attrib={"lat": f"{lat:.7f}", "lon": f"{lon:.7f}"})
        ET.SubElement(trkpt, "ele").text = f"{ele:.1f}"

    ET.indent(gpx, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(gpx, encoding="unicode")


def write_gpx_file(result: dict, path: str, track_name: str = "Ridge Walker Route") -> None:
    """Writes a routing result to a .gpx file."""
    gpx_str = route_to_gpx(result, track_name=track_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(gpx_str)


def redact_api_keys(text: str) -> str:
    """Hides any 'api=<key>' query parameter in a message before it is sent to the user."""
    return re.sub(r'([?&]api=)[^&\s"\']+', r'\1***REDACTED***', text)


# ── AWS Lambda entry point ───────────────────────────────────────────────

ALLOWED_ORIGINS = {
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS", "https://ridgewalker.co.nz,https://www.ridgewalker.co.nz"
    ).split(",") if o.strip()
}


def _request_origin(event: dict) -> str | None:
    """Returns the Origin header from a Lambda request, if present."""
    return (event.get("headers") or {}).get("origin")


def cors_headers_for(origin: str | None) -> dict:
    """Builds CORS headers that echo the caller's origin when it is on the allow-list."""
    allow_origin = origin if origin in ALLOWED_ORIGINS else next(iter(ALLOWED_ORIGINS), "*")
    return {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }


def _json_response(status_code: int, cors_headers: dict, payload: dict) -> dict:
    """Wraps a payload as a JSON Lambda response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {**cors_headers, "Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _warm_up():
    """Loads the water dataset and compiles the A* code in advance, so the first real request is fast."""
    _load_local_water_index()

    tiny_lats = np.array([-41.0, -41.0001, -41.0002])
    tiny_lons = np.array([174.0, 174.0001, 174.0002])
    tiny_elevation = np.zeros((3, 3))
    tiny_costs = compute_directional_costs(
        tiny_elevation, tiny_lats,
        abs(tiny_lats[1] - tiny_lats[0]), abs(tiny_lons[1] - tiny_lons[0]),
    )
    astar_on_grid(tiny_costs, tiny_lats, tiny_lons, (0, 0), (2, 2))


def lambda_handler(event, context):
    """Handles CORS preflight, warm-up pings and route requests from the website."""
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )
    cors_headers = cors_headers_for(_request_origin(event))

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": cors_headers, "body": ""}

    try:
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        payload = json.loads(raw_body)
    except (TypeError, ValueError) as e:
        return _json_response(400, cors_headers, {"ok": False, "error": redact_api_keys(f"Bad request: {e}")})

    if payload.get("warm"):
        try:
            _warm_up()
        except Exception as e:
            print(f"WARNING: warm-up failed ({e}) — the next real request will pay the cold-start cost.")
        return {"statusCode": 200, "headers": cors_headers, "body": json.dumps({"ok": True, "warm": True})}

    try:
        waypoint_a = tuple(payload["a"])
        waypoint_b = tuple(payload["b"])
        via = [tuple(p) for p in payload.get("via", [])] or None
        node_budget = int(payload.get("node_budget", DEFAULT_NODE_BUDGET))
    except (KeyError, TypeError, ValueError) as e:
        return _json_response(400, cors_headers, {"ok": False, "error": redact_api_keys(f"Bad request: {e}")})

    try:
        result = route_with_waypoints(waypoint_a, waypoint_b, via=via, node_budget=node_budget)
        if result.get("ok"):
            result["gpx"] = route_to_gpx(result)
        elif "error" in result:
            result["error"] = redact_api_keys(result["error"])
    except Exception as e:
        return _json_response(500, cors_headers, {"ok": False, "error": redact_api_keys(f"Routing failed: {e}")})

    return _json_response(200 if result.get("ok") else 400, cors_headers, result)


# Name used by the Docker image's CMD ("route_generator.handler").
handler = lambda_handler


# ── Command-line interface ───────────────────────────────────────────────

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
