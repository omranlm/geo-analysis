"""Download a cloud-free Sentinel-2 L2A image clipped to an AOI polygon.

Searches the Element 84 Earth Search STAC API (free, no account needed),
picks the least-cloudy scene that fully covers the AOI, and streams only the
pixels inside the AOI from the Cloud-Optimized GeoTIFFs on AWS. All bands are
resampled onto a common 10 m grid so 20 m and 60 m bands stack with 10 m ones.

Bands (--bands) are a preset or a comma-separated list of band names, e.g.
  --bands rgb                  true colour (default)
  --bands falsecolor           NIR, red, green
  --bands nir,red,green        same as falsecolor
  --bands B08,B04              band IDs work too
  --bands all                  all 12 spectral bands
Run with --list-bands to see every band and preset.

Outputs (in --out-dir), <bands> is the preset name or the list of band names:
  <name>_<date>_<bands>_reflectance.tif   uint16 surface reflectance x 10000, 10 m
  <name>_<date>_<bands>_8bit.tif          uint8 stretched image (1- or 3-band selections only)
  <name>_<date>_<bands>.png               quick-look PNG (1- or 3-band selections only)

Example:
  python download_s2.py my_area.geojson --start 2026-06-01 --end 2026-09-30 --bands falsecolor
"""

import argparse
import importlib.util
import json
import os
import warnings
from datetime import date, timedelta
from pathlib import Path

# Use rasterio's bundled PROJ database; a PROJ_LIB from another install
# (e.g. PostGIS) is often incompatible and breaks CRS lookups.
_proj = Path(importlib.util.find_spec("rasterio").origin).parent / "proj_data"
if _proj.is_dir():
    os.environ["PROJ_DATA"] = os.environ["PROJ_LIB"] = str(_proj)

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.errors import NotGeoreferencedWarning
from rasterio.features import geometry_mask, geometry_window
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom
from pystac_client import Client
from shapely.geometry import shape

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
BOA_OFFSET = 1000  # L2A processing baseline >= 04.00 adds +1000 to DN values
NODATA = 0
GRID_ASSET = "red"  # 10 m band that defines the output grid

# Earth Search asset key -> (band ID, native resolution m, description)
BANDS = {
    "coastal": ("B01", 60, "coastal aerosol 443 nm"),
    "blue": ("B02", 10, "blue 490 nm"),
    "green": ("B03", 10, "green 560 nm"),
    "red": ("B04", 10, "red 665 nm"),
    "rededge1": ("B05", 20, "red edge 704 nm"),
    "rededge2": ("B06", 20, "red edge 740 nm"),
    "rededge3": ("B07", 20, "red edge 783 nm"),
    "nir": ("B08", 10, "NIR 842 nm"),
    "nir08": ("B8A", 20, "narrow NIR 865 nm"),
    "nir09": ("B09", 60, "water vapour 945 nm"),
    "swir16": ("B11", 20, "SWIR 1610 nm"),
    "swir22": ("B12", 20, "SWIR 2190 nm"),
}
BAND_IDS = {bid: key for key, (bid, _, _) in BANDS.items()}

PRESETS = {
    "rgb": (["red", "green", "blue"], "true colour"),
    "falsecolor": (["nir", "red", "green"], "colour infrared: vegetation shows red"),
    "swir": (["swir22", "nir", "red"], "built-up, bare soil and burn scars"),
    "agriculture": (["swir16", "nir", "blue"], "crop health and moisture"),
    "all": (list(BANDS), "all 12 spectral bands"),
}


def parse_bands(spec):
    """Return (asset keys, label) for a preset name or comma-separated band list."""
    spec = spec.strip()
    if spec.lower() in PRESETS:
        return PRESETS[spec.lower()][0], spec.lower()
    keys = []
    for token in (t.strip() for t in spec.split(",") if t.strip()):
        bid = token.upper()
        if len(bid) == 2 and bid[0] == "B":  # allow B4 for B04
            bid = f"B0{bid[1]}"
        key = BAND_IDS.get(bid) or (token.lower() if token.lower() in BANDS else None)
        if key is None:
            raise SystemExit(f"Unknown band '{token}'. Run with --list-bands to see the options.")
        if key not in keys:
            keys.append(key)
    if not keys:
        raise SystemExit("--bands is empty.")
    return keys, "-".join(keys)


def print_band_list():
    print("Bands (use the name or the band ID):")
    for key, (bid, res, desc) in BANDS.items():
        print(f"  {key:<9} {bid:<4} {res:>2} m  {desc}")
    print("\nPresets:")
    for name, (keys, desc) in PRESETS.items():
        shown = ",".join(keys) if name != "all" else "B01-B12 (no B10)"
        print(f"  {name:<12} {shown:<22} {desc}")


def load_aoi(path):
    gj = json.loads(Path(path).read_text(encoding="utf-8"))
    if gj["type"] == "FeatureCollection":
        geom = gj["features"][0]["geometry"]
    elif gj["type"] == "Feature":
        geom = gj["geometry"]
    else:
        geom = gj
    return geom


def find_best_item(aoi_geom, start, end, max_cloud):
    aoi = shape(aoi_geom)
    client = Client.open(STAC_URL)
    search = client.search(
        collections=[COLLECTION],
        intersects=aoi_geom,
        datetime=f"{start}/{end}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        max_items=500,
    )
    items = [it for it in search.items() if shape(it.geometry).contains(aoi)]
    if not items:
        raise SystemExit(
            f"No scenes fully covering the AOI with cloud cover < {max_cloud}% "
            f"between {start} and {end}. Widen the date range or raise --max-cloud."
        )
    items.sort(key=lambda it: (it.properties["eo:cloud_cover"], -it.datetime.timestamp()))
    print(f"Found {len(items)} candidate scenes; best:")
    for it in items[:5]:
        print(f"  {it.id}  {it.datetime:%Y-%m-%d}  cloud={it.properties['eo:cloud_cover']:.2f}%")
    return items[0]


def output_grid(item, aoi_geom):
    """10 m grid (crs, transform, width, height) covering the AOI, aligned to the tile."""
    with rasterio.open(item.assets[GRID_ASSET].href) as src:
        geom = transform_geom("EPSG:4326", src.crs, aoi_geom)
        window = geometry_window(src, [geom])
        return src.crs, src.window_transform(window), int(window.width), int(window.height), geom


def read_band(href, crs, transform, width, height, resampling):
    """Read a band resampled onto the output grid; only the needed blocks are fetched."""
    with rasterio.open(href) as src:
        with WarpedVRT(src, crs=crs, transform=transform, width=width, height=height,
                       resampling=resampling, nodata=NODATA) as vrt:
            return vrt.read(1)


def to_8bit(refl, valid, high=None, gamma=1.0 / 1.4):
    """Scale reflectance (0-1) to 1-255 with a mild gamma; 0 is kept for nodata.

    high: fixed upper reflectance for every band, or None to stretch each band
    to its own 98th percentile inside the AOI.
    """
    out = np.zeros(refl.shape, dtype=np.uint8)
    for i, band in enumerate(refl):
        top = high if high is not None else max(float(np.percentile(band[valid], 98)), 1e-4)
        scaled = np.clip(band / top, 0, 1) ** gamma
        out[i] = (scaled * 254 + 1).astype(np.uint8)
    out[:, ~valid] = 0
    return out


def main():
    today = date.today()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("aoi", nargs="?", help="GeoJSON file with the AOI polygon (EPSG:4326)")
    p.add_argument("--bands", default="rgb", help="preset or comma-separated bands (default: rgb); see --list-bands")
    p.add_argument("--list-bands", action="store_true", help="list available bands and presets, then exit")
    p.add_argument("--start", default=str(today - timedelta(days=90)), help="start date YYYY-MM-DD (default: 90 days ago)")
    p.add_argument("--end", default=str(today), help="end date YYYY-MM-DD (default: today)")
    p.add_argument("--max-cloud", type=float, default=10, help="max scene cloud cover %% (default: 10)")
    p.add_argument("--resampling", default="bilinear", choices=["nearest", "bilinear", "cubic"],
                   help="method for bringing 20/60 m bands to 10 m (default: bilinear)")
    p.add_argument("--out-dir", default="output", help="output folder (default: ./output)")
    p.add_argument("--name", default=None, help="output file prefix (default: AOI file name)")
    args = p.parse_args()

    if args.list_bands:
        print_band_list()
        return
    if not args.aoi:
        p.error("the AOI GeoJSON file is required")

    keys, label = parse_bands(args.bands)
    aoi_geom = load_aoi(args.aoi)
    name = args.name or Path(args.aoi).stem
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    item = find_best_item(aoi_geom, args.start, args.end, args.max_cloud)
    day = f"{item.datetime:%Y%m%d}"
    print(f"Downloading {item.id} ...")

    crs, transform, width, height, geom = output_grid(item, aoi_geom)
    resampling = Resampling[args.resampling]
    bands = []
    for key in keys:
        bid, res, _ = BANDS[key]
        bands.append(read_band(item.assets[key].href, crs, transform, width, height, resampling))
        note = "" if res == 10 else f" (resampled from {res} m)"
        print(f"  {key} ({bid}): {width}x{height} px{note}")
    dn = np.stack(bands)
    inside = geometry_mask([geom], out_shape=(height, width), transform=transform, invert=True)
    valid = inside & np.all(dn != NODATA, axis=0)

    # Earth Search reports whether the +1000 BOA offset was already removed
    offset = 0 if item.properties.get("earthsearch:boa_offset_applied", False) else BOA_OFFSET
    baseline = float(item.properties.get("s2:processing_baseline", "0") or 0)
    if baseline < 4.0:
        offset = 0
    refl = np.clip(dn.astype(np.int32) - offset, 1, None).astype(np.uint16)
    refl[:, ~valid] = NODATA

    profile = dict(
        driver="GTiff", crs=crs, transform=transform, width=width, height=height,
        count=len(keys), nodata=NODATA, compress="deflate", tiled=True,
        blockxsize=256, blockysize=256,
    )
    if len(keys) == 3:
        profile["photometric"] = "RGB"
    descriptions = tuple(f"{BANDS[k][0]} {BANDS[k][2]}" for k in keys)

    saved = []
    refl_path = out_dir / f"{name}_{day}_{label}_reflectance.tif"
    with rasterio.open(refl_path, "w", **profile, dtype="uint16") as dst:
        dst.write(refl)
        dst.descriptions = descriptions
        dst.update_tags(scale_factor="0.0001", stac_item=item.id, cloud_cover=item.properties["eo:cloud_cover"],
                        bands=",".join(BANDS[k][0] for k in keys))
    saved.append(refl_path)

    if len(keys) in (1, 3):
        # Fixed stretch keeps true colour comparable between dates; other
        # combinations (NIR, SWIR) are brighter, so stretch each band to its data.
        img8 = to_8bit(refl.astype(np.float32) / 10000.0, valid, high=0.30 if label == "rgb" else None)
        img8_path = out_dir / f"{name}_{day}_{label}_8bit.tif"
        with rasterio.open(img8_path, "w", **profile, dtype="uint8") as dst:
            dst.write(img8)
            dst.descriptions = descriptions
        png_path = out_dir / f"{name}_{day}_{label}.png"
        warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
        with rasterio.open(png_path, "w", driver="PNG", width=width, height=height,
                           count=len(keys), dtype="uint8") as dst:
            dst.write(img8)
        saved += [img8_path, png_path]
    else:
        print(f"  ({len(keys)} bands selected: preview images are only made for 1 or 3 bands)")

    print("Saved:")
    for path in saved:
        print(f"  {path}")


if __name__ == "__main__":
    main()
