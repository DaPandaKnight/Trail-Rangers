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

Output is still nz_water_data.pkl in the exact same format the original
script produced — (list of shapely Polygon, list of shapely LineString)
— so route_generator.py needs NO changes to use it.

Dependencies: pip install requests shapely pyshp

Usage: python3 prefetch_nz_water_data.py
"""

import pickle
import zipfile
from pathlib import Path

import requests
from shapely.geometry import LineString, shape
import shapefile

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


def main():
    download_geofabrik_extract()
    extract_water_layers()

    print("Parsing water polygons...")
    water_polygons = parse_water_polygons()
    print(f"  {len(water_polygons)} water polygons.")

    print("Parsing stream/river/canal lines...")
    stream_lines = parse_stream_lines()
    print(f"  {len(stream_lines)} stream lines.")

    with open(OUTPUT_FILE, "wb") as f:
        pickle.dump((water_polygons, stream_lines), f)
    print(f"Saved to {OUTPUT_FILE} — route_generator.py will use this instead of live Overpass calls.")


if __name__ == "__main__":
    main()
