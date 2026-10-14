"""Download cloud-free Sentinel-2 L2A imagery clipped to an AOI polygon.

Searches the Element 84 Earth Search STAC API (free, no account needed) for
scenes that fully cover the AOI, and streams only the pixels inside the AOI
from the Cloud-Optimized GeoTIFFs on AWS. All bands are resampled onto a common
10 m grid so 20 m and 60 m bands stack with 10 m ones.

By default the single least-cloudy scene in the date range is downloaded.
With --all-scenes, every matching scene is downloaded (one per date), giving a
time series.

Bands (--bands) are a preset or a comma-separated list of band names, e.g.
  --bands rgb                  true colour (default)
  --bands falsecolor           NIR, red, green
  --bands nir,red,green        same as falsecolor
  --bands B08,B04              band IDs work too
  --bands all                  all 12 spectral bands
Run with --list-bands to see every band and preset.

Cloud filtering:
  --max-cloud      cloud % of the whole Sentinel-2 tile (catalogue metadata, fast)
  --max-aoi-cloud  cloud % inside the AOI, measured from the scene
                   classification (SCL) layer; slower but much more accurate

Outputs (in --out-dir), <bands> is the preset name or the list of band names:
  <name>_<date>_<bands>_reflectance.tif   uint16 surface reflectance x 10000, 10 m
  <name>_<date>_<bands>_8bit.tif          uint8 stretched image (1- or 3-band selections only)
  <name>_<date>_<bands>.png               quick-look PNG (1- or 3-band selections only)
  <name>_scenes.csv                       list of downloaded scenes (--all-scenes only)

Examples:
  python download_s2.py my_area.geojson --start 2026-06-01 --end 2026-09-30 --bands falsecolor
  python download_s2.py my_area.geojson --start 2026-01-01 --end 2026-09-30 --all-scenes --max-aoi-cloud 5
  python download_s2.py my_area.geojson --start 2015-06-01 --all-scenes --max-scenes 0 --resume

If the GeoJSON holds several polygons, they are merged into one AOI.
"""

import argparse
import csv
import importlib.util
import json
import math
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
from rasterio.mask import mask
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom
from pystac_client import Client
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from tqdm import tqdm

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
BOA_OFFSET = 1000  # L2A processing baseline >= 04.00 adds +1000 to DN values
NODATA = 0
GRID_ASSET = "red"  # 10 m band that defines the output grid
DEFAULT_MAX_CLOUD = 10
DEFAULT_MAX_SCENES = 50

# Scene classification (SCL) classes counted as cloud in the AOI check:
# 3 cloud shadow, 8 cloud medium probability, 9 cloud high probability, 10 thin cirrus.
# Class 0 is no data and is excluded from the count.
SCL_CLOUD = [3, 8, 9, 10]

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

SHOW_PROGRESS = True  # switched off by --no-progress


def progress(iterable=None, **kwargs):
    """A tqdm progress bar with the script's defaults."""
    return tqdm(iterable, disable=not SHOW_PROGRESS, dynamic_ncols=True, **kwargs)

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


def _area_m2(geom):
    """Approximate area in m2 of a lon/lat geometry (fine for warnings)."""
    lat = geom.centroid.y
    return geom.area * 111_320**2 * math.cos(math.radians(lat))


def load_aoi(path):
    """Merge every polygon in the GeoJSON into one AOI geometry (Polygon or MultiPolygon)."""
    gj = json.loads(Path(path).read_text(encoding="utf-8"))
    if gj["type"] == "FeatureCollection":
        geoms = [f["geometry"] for f in gj["features"] if f.get("geometry")]
    elif gj["type"] == "Feature":
        geoms = [gj["geometry"]]
    else:
        geoms = [gj]

    polygons = []
    for i, g in enumerate(geoms, 1):
        if g["type"] not in ("Polygon", "MultiPolygon"):
            print(f"Warning: feature {i} is a {g['type']}, not a polygon; ignored.")
            continue
        polygons.append((i, shape(g)))
    if not polygons:
        raise SystemExit(f"{path} contains no polygons.")

    if len(polygons) > 1:
        print(f"Note: {path} has {len(polygons)} polygons; they are merged into one AOI:")
        for i, poly in polygons:
            area = _area_m2(poly)
            # Smaller than one 10 m pixel: almost certainly an accidental click when drawing
            hint = "  <- smaller than one 10 m pixel; probably drawn by accident" if area < 100 else ""
            print(f"  feature {i}: {area / 1e6:.4f} km2 ({area:,.0f} m2){hint}")
    aoi = unary_union([poly for _, poly in polygons])
    return mapping(aoi)


def search_items(aoi_geom, start, end, max_cloud):
    """Scenes that fully cover the AOI, at most one per acquisition date."""
    aoi = shape(aoi_geom)
    client = Client.open(STAC_URL)
    search = client.search(
        collections=[COLLECTION],
        intersects=aoi_geom,
        datetime=f"{start}/{end}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        max_items=2000,
    )
    try:
        total = search.matched()  # None if the API doesn't report a count
    except Exception:
        total = None
    items = []
    with progress(total=total, desc="Searching catalogue", unit="scene") as pbar:
        for it in search.items():
            if shape(it.geometry).contains(aoi):
                items.append(it)
            pbar.update()
            pbar.set_postfix_str(f"{len(items)} cover the AOI")
    # The same date can appear more than once (overlapping tiles or reprocessed
    # versions); keep the least cloudy, preferring the newest processing.
    by_day = {}
    for it in sorted(items, key=lambda it: (it.properties["eo:cloud_cover"], -_version(it))):
        by_day.setdefault(it.datetime.date(), it)
    return sorted(by_day.values(), key=lambda it: it.datetime)


def _version(item):
    """Earth Search processing version: the number before '_L2A' in the item ID."""
    try:
        return int(item.id.rsplit("_", 2)[-2])
    except ValueError:
        return 0


def aoi_cloud_percent(item, aoi_geom):
    """Percentage of AOI pixels classed as cloud, cloud shadow or cirrus in SCL."""
    with rasterio.open(item.assets["scl"].href) as src:
        geom = transform_geom("EPSG:4326", src.crs, aoi_geom)
        scl, _ = mask(src, [geom], crop=True, nodata=0, filled=True)
    scl = scl[0]
    data = scl != 0
    if not data.any():
        return 100.0
    return float(np.isin(scl[data], SCL_CLOUD).mean() * 100)


def add_aoi_cloud(items, aoi_geom, max_aoi_cloud):
    """Measure AOI cloud for each item and keep those at or below the limit."""
    kept = []
    with progress(items, desc="Checking AOI cloud", unit="scene") as pbar:
        for it in pbar:
            try:
                pct = aoi_cloud_percent(it, aoi_geom)
            except Exception as exc:  # network hiccup or missing SCL: skip the scene
                tqdm.write(f"  {it.id}: AOI cloud check failed ({exc}); skipped")
                continue
            it.properties["aoi_cloud"] = pct
            status = "ok" if pct <= max_aoi_cloud else "too cloudy"
            tqdm.write(f"  {it.datetime:%Y-%m-%d}  tile={it.properties['eo:cloud_cover']:6.2f}%  "
                       f"AOI={pct:6.2f}%  {status}")
            if pct <= max_aoi_cloud:
                kept.append(it)
            pbar.set_postfix_str(f"{len(kept)} clear")
    return kept


def describe(item):
    text = f"{item.id}  {item.datetime:%Y-%m-%d}  tile cloud={item.properties['eo:cloud_cover']:.2f}%"
    if "aoi_cloud" in item.properties:
        text += f"  AOI cloud={item.properties['aoi_cloud']:.2f}%"
    return text


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


def download_scene(item, aoi_geom, keys, label, name, out_dir, resampling, verbose=True):
    """Download the requested bands of one scene; return the list of files written.

    verbose: print scene details and keep the progress bar afterwards; off when
    this is one scene of a series, which has its own overall bar.
    """
    day = f"{item.datetime:%Y%m%d}"
    previews = len(keys) in (1, 3)
    if verbose:
        print(f"Downloading {describe(item)}")
    steps = 1 + len(keys) + 1 + previews  # grid, each band, reflectance file, previews
    pbar = progress(total=steps, desc=f"Downloading {item.datetime:%Y-%m-%d}", unit="step", leave=verbose)
    with pbar:
        pbar.set_postfix_str("output grid")
        crs, transform, width, height, geom = output_grid(item, aoi_geom)
        pbar.update()
        if verbose:
            resampled = [f"{BANDS[k][0]} ({BANDS[k][1]} m)" for k in keys if BANDS[k][1] != 10]
            tqdm.write(f"  {len(keys)} bands on a {width}x{height} px 10 m grid"
                       + (f"; resampled to 10 m: {', '.join(resampled)}" if resampled else ""))

        bands = []
        for key in keys:
            pbar.set_postfix_str(f"{key} ({BANDS[key][0]})")
            bands.append(read_band(item.assets[key].href, crs, transform, width, height, resampling))
            pbar.update()
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
        tags = dict(scale_factor="0.0001", stac_item=item.id, cloud_cover=item.properties["eo:cloud_cover"],
                    bands=",".join(BANDS[k][0] for k in keys))
        if "aoi_cloud" in item.properties:
            tags["aoi_cloud_cover"] = round(item.properties["aoi_cloud"], 2)

        pbar.set_postfix_str("writing reflectance GeoTIFF")
        saved = []
        refl_path = reflectance_path(item, name, label, out_dir)
        # Written under a temporary name and renamed once the scene is complete,
        # so an interrupted run never leaves a file that --resume would skip.
        part_path = refl_path.with_name(refl_path.stem + ".part.tif")
        with rasterio.open(part_path, "w", **profile, dtype="uint16") as dst:
            dst.write(refl)
            dst.descriptions = descriptions
            dst.update_tags(**tags)
        saved.append(refl_path)
        pbar.update()

        if previews:
            pbar.set_postfix_str("writing previews")
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
            pbar.update()
        elif verbose:
            tqdm.write(f"  ({len(keys)} bands selected: preview images are only made for 1 or 3 bands)")
        os.replace(part_path, refl_path)
        pbar.set_postfix_str("done")
    return saved


def reflectance_path(item, name, label, out_dir):
    """The reflectance GeoTIFF for a scene; its presence marks the scene as downloaded."""
    return out_dir / f"{name}_{item.datetime:%Y%m%d}_{label}_reflectance.tif"


def _saved_aoi_cloud(item, name, label, out_dir):
    """AOI cloud % stored in an existing reflectance file (also set on the item), or None."""
    with rasterio.open(reflectance_path(item, name, label, out_dir)) as src:
        value = src.tags().get("aoi_cloud_cover")
    if value is None:
        return None
    item.properties["aoi_cloud"] = float(value)
    return item.properties["aoi_cloud"]


def scene_row(item, refl_path):
    """One row of the --all-scenes CSV."""
    aoi_cloud = item.properties.get("aoi_cloud")
    return [f"{item.datetime:%Y-%m-%d}", item.id, round(item.properties["eo:cloud_cover"], 2),
            "" if aoi_cloud is None else round(aoi_cloud, 2), refl_path.name]


def main():
    today = date.today()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("aoi", nargs="?", help="GeoJSON file with the AOI polygon (EPSG:4326)")
    p.add_argument("--bands", default="rgb", help="preset or comma-separated bands (default: rgb); see --list-bands")
    p.add_argument("--list-bands", action="store_true", help="list available bands and presets, then exit")
    p.add_argument("--start", default=str(today - timedelta(days=90)), help="start date YYYY-MM-DD (default: 90 days ago)")
    p.add_argument("--end", default=str(today), help="end date YYYY-MM-DD (default: today)")
    p.add_argument("--max-cloud", type=float, default=None,
                   help=f"max cloud %% of the whole tile (default: {DEFAULT_MAX_CLOUD}, "
                        "or 100 when --max-aoi-cloud is used)")
    p.add_argument("--max-aoi-cloud", type=float, default=None,
                   help="max cloud %% inside the AOI, measured from the SCL layer (default: not checked)")
    p.add_argument("--all-scenes", action="store_true",
                   help="download every matching scene (one per date) instead of only the best one")
    p.add_argument("--max-scenes", type=int, default=DEFAULT_MAX_SCENES,
                   help=f"with --all-scenes, stop if more scenes than this match (default: {DEFAULT_MAX_SCENES}; 0 = no limit)")
    p.add_argument("--resampling", default="bilinear", choices=["nearest", "bilinear", "cubic"],
                   help="method for bringing 20/60 m bands to 10 m (default: bilinear)")
    p.add_argument("--out-dir", default="output", help="output folder (default: ./output)")
    p.add_argument("--name", default=None, help="output file prefix (default: AOI file name)")
    p.add_argument("--resume", action="store_true",
                   help="skip scenes whose reflectance GeoTIFF already exists in --out-dir (to continue an interrupted run)")
    p.add_argument("--no-progress", action="store_true", help="hide progress bars (e.g. when logging to a file)")
    args = p.parse_args()

    global SHOW_PROGRESS
    SHOW_PROGRESS = not args.no_progress

    if args.list_bands:
        print_band_list()
        return
    if not args.aoi:
        p.error("the AOI GeoJSON file is required")

    # The AOI check is the accurate one, so by default it replaces the tile filter
    if args.max_cloud is None:
        args.max_cloud = 100 if args.max_aoi_cloud is not None else DEFAULT_MAX_CLOUD

    keys, label = parse_bands(args.bands)
    aoi_geom = load_aoi(args.aoi)
    name = args.name or Path(args.aoi).stem
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    resampling = Resampling[args.resampling]

    items = search_items(aoi_geom, args.start, args.end, args.max_cloud)
    print(f"Found {len(items)} scenes (one per date) fully covering the AOI between {args.start} and {args.end} "
          f"with tile cloud < {args.max_cloud:g}%.")
    existing = set()
    if args.resume:
        existing = {it.id for it in items if reflectance_path(it, name, label, out_dir).exists()}
        if existing:
            print(f"Resuming: {len(existing)} of these scenes are already in {out_dir} and will be skipped.")
    if items and args.max_aoi_cloud is not None:
        # Already-downloaded scenes carry their AOI cloud in the file's tags; reuse it
        known = [it for it in items if it.id in existing and _saved_aoi_cloud(it, name, label, out_dir) is not None]
        to_check = [it for it in items if it not in known]
        kept_known = [it for it in known if it.properties["aoi_cloud"] <= args.max_aoi_cloud]
        if known:
            print(f"AOI cloud read from {len(known)} existing files; checking the other {len(to_check)} scenes.")
        checked = add_aoi_cloud(to_check, aoi_geom, args.max_aoi_cloud) if to_check else []
        items = sorted(kept_known + checked, key=lambda it: it.datetime)
        print(f"{len(items)} scenes have AOI cloud <= {args.max_aoi_cloud:g}%.")
    if not items:
        limits = f"tile cloud < {args.max_cloud:g}%"
        if args.max_aoi_cloud is not None:
            limits += f" and AOI cloud <= {args.max_aoi_cloud:g}%"
        raise SystemExit(f"No scenes match ({limits}). Widen the date range or relax the cloud limits.")

    if not args.all_scenes:
        cloud = (lambda it: (it.properties["aoi_cloud"], it.properties["eo:cloud_cover"])) \
            if args.max_aoi_cloud is not None else (lambda it: (it.properties["eo:cloud_cover"],))
        ranked = sorted(items, key=lambda it: (*cloud(it), -it.datetime.timestamp()))
        print("Best scenes:")
        for it in ranked[:5]:
            print(f"  {describe(it)}")
        if ranked[0].id in existing:
            print(f"Already downloaded (--resume): {reflectance_path(ranked[0], name, label, out_dir)}")
            return
        saved = download_scene(ranked[0], aoi_geom, keys, label, name, out_dir, resampling)
        print("Saved:")
        for path in saved:
            print(f"  {path}")
        return

    todo = [it for it in items if it.id not in existing]
    if args.max_scenes and len(todo) > args.max_scenes:
        still = " still to download" if existing else ""
        raise SystemExit(
            f"{len(todo)} scenes{still} match, more than --max-scenes {args.max_scenes}. Narrow the date range, "
            f"tighten the cloud limits, or raise --max-scenes (0 = no limit)."
        )
    print(f"Downloading {len(todo)} scenes (oldest first)"
          + (f"; {len(existing)} already downloaded" if existing else "") + " ...")
    csv_path = out_dir / f"{name}_scenes.csv"
    failed = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "stac_item", "tile_cloud_percent", "aoi_cloud_percent", "reflectance_file"])
        with progress(items, desc="Scenes", unit="scene") as scenes:
            for it in scenes:
                if it.id in existing:
                    writer.writerow(scene_row(it, reflectance_path(it, name, label, out_dir)))
                    continue
                try:
                    saved = download_scene(it, aoi_geom, keys, label, name, out_dir, resampling, verbose=False)
                except Exception as exc:  # keep going; one bad scene shouldn't stop a long series
                    failed += 1
                    tqdm.write(f"  FAILED {describe(it)}: {exc}")
                    continue
                tqdm.write(f"  saved {describe(it)}")
                writer.writerow(scene_row(it, saved[0]))
                scenes.set_postfix_str(f"{failed} failed" if failed else "")
    print(f"Done: {len(todo) - failed} new scenes saved to {out_dir}"
          + (f", {len(existing)} already there" if existing else "")
          + (f", {failed} failed (run again with --resume to retry them)" if failed else "") + ".")
    print(f"Scene list: {csv_path}")


if __name__ == "__main__":
    main()
