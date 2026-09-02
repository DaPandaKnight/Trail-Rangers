"""
prefetch_nz_water_data.py — ONE-TIME build step for a local water/stream
dataset covering New Zealand.

REWRITTEN: the original version of this script tiled NZ into ~180 pieces
and fetched each via live Overpass queries. In practice that was
unreliable — Overpass returned a mix of 504 Gateway Timeouts and 429 Too
Many Requests errors, because its live query API is explicitly meant for
interactive/exploratory queries, not bulk regional data extraction (this
is stated in Overpass's own usage policy, not just something inferred
from the errors).

The correct tool for "download all of a region's OSM data once" is a
proper OSM data extract — a static file, not a live API. This version
downloads New Zealand's water/waterway data as a single shapefile package
from Geofabrik (a well-established, purpose-built OSM extract provider),
then parses it locally. One large download instead of ~180 unreliable
live queries.

OCEAN/SEA COVERAGE (added later): Geofabrik's water layer only covers
inland water bodies (lakes, reservoirs) — it does not include the ocean,
so a route could still be found crossing open water. Ocean/sea polygons
are fetched separately from osmdata.openstreetmap.de's "water polygons"
dataset (derived from OSM's natural=coastline data, NOT the same as
Geofabrik's inland-water layer — this one is oceans/seas specifically,
explicitly excluding lakes/reservoirs per that project's own
documentation). Ocean polygons are merged into the SAME water_polygons
list Geofabrik's lakes/reservoirs go into — route_generator.py's existing
water-masking logic (apply_water_mask) treats every entry in that list
identically, so no changes were needed there to add ocean blocking.

CONFIRMED URL: OCEAN_POLYGONS_URL below was initially inferred by pattern-
matching osmdata.openstreetmap.de's "land-polygons-split-4326.zip" naming
convention, then verified by hand against the real Water polygons page
(https://osmdata.openstreetmap.de/data/water-polygons.html) — the "Format:
Shapefile, Projection: WGS84 (Large polygons are split)" download link
matches exactly. Not fetched and test-parsed end-to-end from this
environment (which can't reach that host), so download_ocean_polygons()
still fails with a clear, actionable error rather than silently producing
an empty result if anything about the actual file contents surprises us.

Output is still nz_water_data.pkl in the exact same format the original
script produced — (list of shapely Polygon, list of shapely LineString)
— so route_generator.py needs NO changes to use it.

Dependencies: pip install requests shapely pyshp

Usage: python3 prefetch_nz_water_data.py
"""

import math
import pickle
import time
import zipfile
from pathlib import Path

import requests
from shapely.geometry import LineString, box, shape
from shapely.ops import unary_union
import shapefile

# Same representative-latitude approach route_generator.py used when this
# was computed at cold-start — kept identical here so the precomputed
# result means the same thing either way. See main()'s comment for why
# this moved here.
NZ_REPRESENTATIVE_LAT = -41.0
STREAM_BUFFER_M = 15.0  # must match route_generator.py's STREAM_BUFFER_M


def meters_per_degree(lat: float) -> tuple[float, float]:
    """Same formula as route_generator.py's — duplicated here to keep this script standalone, no import dependency on it."""
    return 111_320.0, 111_320.0 * math.cos(math.radians(lat))

# Geofabrik's free NZ shapefile extract — updated roughly daily.
# ~700MB; this is a genuine one-time bulk download, not something to
# re-run casually. See module docstring for why this replaces the
# earlier tiled-Overpass approach.
GEOFABRIK_URL = "https://download.geofabrik.de/australia-oceania/new-zealand-latest-free.shp.zip"

DOWNLOAD_FILE = Path("new-zealand-latest-free.shp.zip")
EXTRACT_DIR = Path("nz_shapefile_extract")
OUTPUT_FILE = Path("nz_water_data.pkl")

# Geofabrik's standard "OpenStreetMap Data in Layered GIS Format" layer
# names — a well-established, documented naming convention used across
# all their country extracts, not specific to NZ.
WATER_LAYER = "gis_osm_water_a_free_1"        # water body polygons (lakes, reservoirs, etc.)
WATERWAYS_LAYER = "gis_osm_waterways_free_1"  # waterway lines

# Matches the scope of the original Overpass-based query: rivers, streams,
# and canals, but not drains/ditches (which the waterways layer also
# contains, tagged with these same fclass values).
STREAM_FCLASSES = {"river", "stream", "canal"}

SHAPEFILE_EXTENSIONS = [".shp", ".shx", ".dbf", ".prj", ".cpg"]

# ── Ocean/sea coverage — URL confirmed against the real osmdata.openstreetmap.de page ──
OCEAN_POLYGONS_URL = "https://osmdata.openstreetmap.de/download/water-polygons-split-4326.zip"
OCEAN_DOWNLOAD_FILE = Path("water-polygons-split-4326.zip")
OCEAN_EXTRACT_DIR = Path("ocean_polygons_extract")

# Padded a bit beyond the routing system's own NZ extent (see
# DEFAULT_NODE_BUDGET's neighbourhood in route_generator.py) so ocean
# polygons are available for any bbox the padding-retry logic might grow
# into, not just the tightest default search area.
NZ_BBOX = (-48.5, 165.0, -33.0, 180.0)  # min_lat, min_lon, max_lat, max_lon


def download_geofabrik_extract():
    if DOWNLOAD_FILE.exists():
        print(f"{DOWNLOAD_FILE} already downloaded, skipping (delete it to force a re-download).")
        return

    print("Downloading NZ OSM extract from Geofabrik (~700MB, this will take a while)...")
    with requests.get(GEOFABRIK_URL, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total_size = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(DOWNLOAD_FILE, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size:
                    pct = 100 * downloaded / total_size
                    print(f"\r  {downloaded / 1e6:.0f}MB / {total_size / 1e6:.0f}MB ({pct:.1f}%)",
                          end="", flush=True)
    print("\nDownload complete.")


def extract_water_layers():
    print(f"Extracting water/waterway layers from {DOWNLOAD_FILE}...")
    EXTRACT_DIR.mkdir(exist_ok=True)

    wanted_prefixes = (WATER_LAYER, WATERWAYS_LAYER)
    with zipfile.ZipFile(DOWNLOAD_FILE) as zf:
        members = [
            name for name in zf.namelist()
            if any(name.startswith(prefix) for prefix in wanted_prefixes)
            and Path(name).suffix in SHAPEFILE_EXTENSIONS
        ]
        if not members:
            raise RuntimeError(
                f"No files matching {wanted_prefixes} found in the archive. "
                f"Geofabrik may have changed their layer naming — inspect "
                f"the zip's contents with `unzip -l {DOWNLOAD_FILE}` and "
                f"update WATER_LAYER/WATERWAYS_LAYER above."
            )
        zf.extractall(EXTRACT_DIR, members=members)
        print(f"Extracted {len(members)} files: {members}")


def parse_water_polygons() -> list:
    """
    Reads the water polygon layer and returns a flat list of shapely
    Polygons — MultiPolygon records (multi-part shapefile features, e.g.
    two disjoint lake basins stored as one record) are split into
    individual Polygons, matching the flat list shape route_generator.py
    already expects. Verified against synthetic test shapefiles covering
    simple polygons, polygons with holes, and multi-part records before
    being trusted on the real data.
    """
    path = EXTRACT_DIR / WATER_LAYER
    reader = shapefile.Reader(str(path))
    polygons = []
    for shp_rec in reader.iterShapeRecords():
        geom = shape(shp_rec.shape.__geo_interface__)
        if geom.is_empty:
            continue
        if geom.geom_type == "Polygon":
            polygons.append(geom)
        elif geom.geom_type == "MultiPolygon":
            polygons.extend(list(geom.geoms))
    reader.close()
    return polygons


def parse_stream_lines() -> list:
    """Reads the waterways layer, keeping only river/stream/canal features (matching the original Overpass query's scope)."""
    path = EXTRACT_DIR / WATERWAYS_LAYER
    reader = shapefile.Reader(str(path))
    lines = []
    for shp_rec in reader.iterShapeRecords():
        fclass = shp_rec.record["fclass"]
        if fclass not in STREAM_FCLASSES:
            continue
        if not shp_rec.shape.points:
            continue
        line = LineString(shp_rec.shape.points)
        if line.is_valid and line.length > 0:
            lines.append(line)
    reader.close()
    return lines


def download_ocean_polygons():
    if OCEAN_DOWNLOAD_FILE.exists():
        print(f"{OCEAN_DOWNLOAD_FILE} already downloaded, skipping "
              f"(delete it to force a re-download).")
        return

    print(f"Downloading global ocean/sea polygons from {OCEAN_POLYGONS_URL} "
          f"(this is a planet-scale dataset — likely a large download; we "
          f"only keep the NZ-area subset once parsed)...")
    try:
        with requests.get(OCEAN_POLYGONS_URL, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total_size = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(OCEAN_DOWNLOAD_FILE, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        pct = 100 * downloaded / total_size
                        print(f"\r  {downloaded / 1e6:.0f}MB / {total_size / 1e6:.0f}MB "
                              f"({pct:.1f}%)", end="", flush=True)
        print("\nDownload complete.")
    except requests.HTTPError as e:
        raise RuntimeError(
            f"Failed to download ocean polygons from {OCEAN_POLYGONS_URL} ({e}). "
            f"This URL was inferred by pattern-matching, not confirmed directly — "
            f"check https://osmdata.openstreetmap.de/data/water-polygons.html "
            f"by hand for the current correct download link and update "
            f"OCEAN_POLYGONS_URL above."
        ) from e


def extract_ocean_layer() -> str:
    """
    Extracts the ocean/sea shapefile from the downloaded zip and returns
    the path (without extension) to pass to shapefile.Reader. The exact
    internal filename inside this archive isn't confirmed (see module
    docstring) — this searches for whatever .shp file it actually
    contains rather than assuming a specific name, so a differently-named
    file still works without needing a code change.
    """
    OCEAN_EXTRACT_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(OCEAN_DOWNLOAD_FILE) as zf:
        shp_members = [name for name in zf.namelist() if name.endswith(".shp")]
        if not shp_members:
            raise RuntimeError(
                f"No .shp file found inside {OCEAN_DOWNLOAD_FILE} — inspect its "
                f"contents with `unzip -l {OCEAN_DOWNLOAD_FILE}` to see what's "
                f"actually in there."
            )
        shp_name = shp_members[0]
        base_name = shp_name[:-4]  # strip ".shp"
        members = [
            name for name in zf.namelist()
            if name.startswith(base_name) and Path(name).suffix in SHAPEFILE_EXTENSIONS
        ]
        zf.extractall(OCEAN_EXTRACT_DIR, members=members)
        print(f"Extracted ocean shapefile: {members}")
        return str(OCEAN_EXTRACT_DIR / base_name)


def parse_ocean_polygons(shapefile_path: str) -> list:
    """
    Reads the ocean/sea polygon layer, keeping only features that
    intersect NZ_BBOX (this is a global dataset — most of it is
    irrelevant to us) and clipping each kept feature down to that bbox,
    so we're not carrying planet-scale vertex complexity for a feature
    that happens to touch our corner of the map. Same MultiPolygon-
    splitting behavior as parse_water_polygons(), for consistency.
    """
    nz_box = box(NZ_BBOX[1], NZ_BBOX[0], NZ_BBOX[3], NZ_BBOX[2])  # (minx, miny, maxx, maxy)

    reader = shapefile.Reader(shapefile_path)
    polygons = []
    total_features = 0
    for shp_rec in reader.iterShapeRecords():
        total_features += 1
        bbox = shp_rec.shape.bbox  # [minx, miny, maxx, maxy] — cheap pre-filter before full parse
        if bbox is None:
            continue
        feature_box = box(bbox[0], bbox[1], bbox[2], bbox[3])
        if not feature_box.intersects(nz_box):
            continue

        geom = shape(shp_rec.shape.__geo_interface__)
        if geom.is_empty:
            continue
        clipped = geom.intersection(nz_box)
        if clipped.is_empty:
            continue

        if clipped.geom_type == "Polygon":
            polygons.append(clipped)
        elif clipped.geom_type == "MultiPolygon":
            polygons.extend(list(clipped.geoms))
        # GeometryCollection (possible after intersection) — keep only the
        # polygonal parts, silently drop any stray point/line slivers.
        elif clipped.geom_type == "GeometryCollection":
            polygons.extend([g for g in clipped.geoms if g.geom_type == "Polygon"])
    reader.close()

    print(f"  Scanned {total_features} global ocean features, kept {len(polygons)} "
          f"intersecting/clipped to the NZ area.")
    return polygons


def main():
    download_geofabrik_extract()
    extract_water_layers()

    print("Parsing water polygons...")
    water_polygons = parse_water_polygons()
    print(f"  {len(water_polygons)} water polygons.")

    print("Parsing stream/river/canal lines...")
    stream_lines = parse_stream_lines()
    print(f"  {len(stream_lines)} stream lines.")

    print("\nFetching ocean/sea coverage...")
    download_ocean_polygons()
    ocean_shapefile_path = extract_ocean_layer()
    ocean_polygons = parse_ocean_polygons(ocean_shapefile_path)
    print(f"  {len(ocean_polygons)} ocean polygons (clipped to NZ area).")

    # Merged into the SAME list Geofabrik's lakes/reservoirs go into —
    # route_generator.py's apply_water_mask() treats every polygon in this
    # list identically, so ocean blocking needed no changes there.
    water_polygons.extend(ocean_polygons)
    print(f"\nTotal water_polygons after merging ocean coverage: {len(water_polygons)}")

    # ── Precompute both global unions HERE, offline, not at Lambda
    # cold-start. This was moved here after a real production run showed
    # the stream buffer union alone taking 677 SECONDS (over 11 minutes)
    # at cold-start — almost certainly enough to exceed a typical Lambda
    # timeout and fail the function outright, not just run slowly. An
    # 11-minute wait is completely fine here, in an offline batch script
    # you run once; it is not fine blocking a live user's first request.
    # route_generator.py now just loads these precomputed results directly
    # from the pickle instead of computing them itself.
    print("\nPrecomputing global water union (this is the expensive part — "
          "may take a while, that's expected and fine here)...")
    t0 = time.time()
    global_water_union = unary_union(water_polygons) if water_polygons else None
    print(f"  Done in {time.time() - t0:.1f}s.")

    print("Precomputing global stream buffer union (this was the SLOWEST step "
          "in production — 677s observed for ~94k stream lines — expect this "
          "to take a while)...")
    t0 = time.time()
    global_stream_buffer_union = None
    if stream_lines:
        merged_streams = unary_union(stream_lines)
        m_per_deg_lat, m_per_deg_lon = meters_per_degree(NZ_REPRESENTATIVE_LAT)
        buffer_deg = STREAM_BUFFER_M / min(m_per_deg_lat, m_per_deg_lon)
        global_stream_buffer_union = merged_streams.buffer(buffer_deg)
    print(f"  Done in {time.time() - t0:.1f}s.")

    with open(OUTPUT_FILE, "wb") as f:
        pickle.dump(
            (water_polygons, stream_lines, global_water_union, global_stream_buffer_union),
            f,
        )
    print(f"Saved to {OUTPUT_FILE} (now includes precomputed unions) — "
          f"route_generator.py will load these directly instead of computing "
          f"them at cold-start.")


if __name__ == "__main__":
    main()
