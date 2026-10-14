# Sentinel-2 Downloader

Download cloud-free Sentinel-2 imagery for **any area of interest (AOI)** defined by a GeoJSON polygon. The default output is true colour (RGB). You can also download any of the 12 spectral bands, or presets such as false colour and SWIR.

The script searches the free [Element 84 Earth Search](https://earth-search.aws.element84.com/v1) catalogue of Sentinel-2 Level-2A imagery hosted on AWS. It downloads either the least-cloudy scene that fully covers your polygon or, with `--all-scenes`, every matching scene as a time series. It can also measure cloud cover over your own area, not just over the whole satellite tile. Only the pixels inside your polygon are downloaded, so a city-sized area is a few MB per scene, not a full ~1 GB scene. No account or API key is needed.

A worked example for Douma, Syria, is included in [`examples/douma/`](examples/douma/).

> **Project context:** this downloader is the **first step of a larger plan** for in-depth geospatial analysis of Syria. It provides the foundation for that work: reliable, repeatable access to analysis-ready Sentinel-2 imagery for any area. The tool itself works anywhere in the world. See [Project scope and next steps](#project-scope-and-next-steps).

| True colour (`--bands rgb`, default) | False colour (`--bands falsecolor`) |
|---|---|
| ![Sentinel-2 true-colour image of Douma, Syria, 13 Sep 2026](examples/douma/douma_20260913_rgb.png) | ![Sentinel-2 false-colour image of Douma, Syria, 13 Sep 2026: vegetation in red](examples/douma/douma_20260913_falsecolor.png) |

*Example output: Douma, Syria, 13 September 2026 (10 m, cloud-free). In false colour, vegetation shows red.*

---

## Project contents

| Path | Description |
|---|---|
| `download_s2.py` | Script that searches for and downloads Sentinel-2 imagery for a polygon |
| `requirements.txt` | Python dependencies |
| `examples/` | Example AOIs with notes and sample output (currently [Douma, Syria](examples/douma/)) |
| `output/` | Downloaded images (created on first run, ignored by git) |

---

## Project scope and next steps

This repository is an **initial step** towards a larger effort: in-depth analysis of Syrian geography using satellite imagery.

**This step provides:**
- a generic, tested tool for downloading cloud-free Sentinel-2 imagery (true colour, any spectral band or band composite) clipped to any polygon;
- a documented method for building an area-of-interest polygon where no official boundary exists (see the [Douma example](examples/douma/));
- a first worked example, Douma in Rif Dimashq, with sample outputs for every band.

**What comes next:** the later stages will build on this foundation to carry out much deeper analysis of Syrian geography. Their specific goals, study areas and methods will be documented here as they are defined.

The downloader deliberately has no Syria-specific logic, so the same tool can serve every later stage and every study area.

---

## Requirements

- **Python 3.10 or newer**, available from [python.org](https://www.python.org/downloads/).
- **An internet connection.** The script queries the STAC API and reads Cloud-Optimized GeoTIFFs from AWS S3 over HTTPS.
- **These Python packages**, listed in `requirements.txt`:

  | Package | Purpose |
  |---|---|
  | `pystac-client` | Searches the Earth Search STAC catalogue |
  | `rasterio` | Reads the remote GeoTIFFs, clips them and writes the outputs. The pip wheel includes GDAL and PROJ, so you don't need to install GDAL separately. |
  | `shapely` | Checks that a scene fully covers the AOI |
  | `numpy` | Array processing |
  | `tqdm` | Progress bars |

---

## Installation

### 1. Open a terminal in the project folder

```bash
cd path/to/sentinel2
```

### 2. Create a virtual environment

A virtual environment keeps this project's packages separate from the rest of your system.

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell blocks the activation script, run this once and try again:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

**Windows (Command Prompt):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

When the environment is active, your prompt starts with `(.venv)`.

### 3. Install the dependencies

```bash
pip install -r requirements.txt
```

### 4. Check the installation

```bash
python -c "import rasterio, pystac_client, shapely, tqdm; print('OK')"
```

---

## Preparing your area of interest

The script needs a GeoJSON file containing your area as **polygons** in **EPSG:4326** (longitude/latitude). Any of these forms is accepted:

- a `FeatureCollection`
- a single `Feature`
- a bare `Polygon` or `MultiPolygon` geometry

**Several polygons in one file** are merged into a single area of interest, and the output covers all of them. When a file has more than one polygon, the script lists each one with its area. It flags any polygon smaller than one 10 m pixel, which is usually a stray click while drawing:

```
Note: near-douma.geojson has 2 polygons; they are merged into one AOI:
  feature 1: 0.0000 km2 (12 m2)  <- smaller than one 10 m pixel; probably drawn by accident
  feature 2: 0.2828 km2 (282,833 m2)
```

If you see a polygon you didn't mean to draw, delete it from the file and run again. Points and lines in the file are ignored, with a warning.

Ways to get one:

- **Draw it** at [geojson.io](https://geojson.io), then use *Save → GeoJSON*.
- **Export it from QGIS:** right-click the layer, choose *Export → Save Features As…*, set the format to *GeoJSON* and the CRS to *EPSG:4326*.
- **Use a boundary from OpenStreetMap.** Search for the place on [nominatim.openstreetmap.org](https://nominatim.openstreetmap.org/); if it has a boundary polygon, download it as GeoJSON. Many towns are mapped only as points. The [Douma example](examples/douma/) shows how to build a polygon in that case.
- **Write a rectangle by hand.** Replace the coordinates below with your own, given as `[lon, lat]` pairs, with the first point repeated at the end:

  ```json
  {
    "type": "Polygon",
    "coordinates": [[[36.37, 33.55], [36.44, 33.55], [36.44, 33.60], [36.37, 33.60], [36.37, 33.55]]]
  }
  ```

**Size limit:** the AOI must fit inside **a single Sentinel-2 tile** (about 110 × 110 km). For larger areas, split the AOI into pieces and run the script for each piece.

---

## Running the download

### Basic usage

```bash
python download_s2.py path/to/my_area.geojson
```

This searches the **last 90 days** for scenes with **less than 10% cloud cover**, downloads the **single best one** and saves the results in `./output`, named after the AOI file.

### Choosing a date range

```bash
python download_s2.py my_area.geojson --start 2026-06-01 --end 2026-09-30
```

By default, the date range is a **search window**: the script looks at every scene in it but downloads only the least-cloudy one. To download every scene in the window, see [Downloading a time series](#downloading-a-time-series).

### Filtering clouds over your area

Sentinel-2 scenes are tiles of about 110 × 110 km, and the catalogue's cloud figure (`--max-cloud`) is for the **whole tile**. Your area may be a small part of it, so this figure can be misleading in both directions: a "10% cloudy" tile can have its one cloud right over your area, and a "40% cloudy" tile can be perfectly clear over it.

`--max-aoi-cloud` measures cloud **inside your polygon** for each candidate scene, using the scene classification layer (SCL) that comes with every Level-2A scene. Pixels classified as cloud (medium or high probability), thin cirrus or **cloud shadow** count as cloudy.

```bash
# Only accept scenes with at most 5% cloud over the AOI
python download_s2.py my_area.geojson --start 2026-01-01 --end 2026-02-28 --max-aoi-cloud 5
```

```
Found 14 scenes (one per date) fully covering the AOI between 2026-01-01 and 2026-02-28 with tile cloud < 100%.
  2026-01-04  tile= 15.17%  AOI=  0.00%  ok
  2026-01-06  tile= 40.76%  AOI=100.00%  too cloudy
  ...
  2026-02-25  tile= 24.14%  AOI= 39.55%  too cloudy
  2026-02-28  tile=  9.22%  AOI=  0.00%  ok
Checking AOI cloud: 100%|██████████| 14/14 [00:41<00:00,  2.95s/scene, 3 clear]
3 scenes have AOI cloud <= 5%.
```

In this Douma example, two of the three clear scenes (4 Jan and 18 Feb) have tile cloud above 10%, so the default tile filter would have rejected them.

How it combines with `--max-cloud`:
- When `--max-aoi-cloud` is set, `--max-cloud` defaults to **100** (no tile filter), so no clear scene is missed. You can still set both; `--max-cloud 60` speeds things up by skipping very cloudy tiles before the AOI check.
- When a single scene is downloaded, the best one is the scene with the **lowest AOI cloud**; ties are broken by tile cloud, then by the most recent date.
- The check reads the small SCL layer for every candidate scene, which takes a few seconds per scene, depending on your connection. For long date ranges, a loose `--max-cloud` keeps it quick.

### Downloading a time series

Add `--all-scenes` to download **every** scene that matches, instead of only the best one. This is the starting point for change analysis, such as seasonal vegetation, urban growth, or damage and reconstruction.

```bash
# Every scene from 2026 with at most 5% cloud over the AOI
python download_s2.py my_area.geojson --start 2026-01-01 --end 2026-09-30 --all-scenes --max-aoi-cloud 5
```

- **One scene per date.** If the same date appears more than once (overlapping tiles or reprocessed versions), only the least-cloudy copy is kept.
- **Oldest first.** Scenes are downloaded in date order, each into its own dated files.
- **Scene list.** A `<name>_scenes.csv` file lists every downloaded scene with its date, STAC item ID, tile cloud, AOI cloud and reflectance file name, ready to load into pandas, R or a spreadsheet.
- **Safety limit.** If more than `--max-scenes` scenes match (default **50**), the script stops before downloading anything and tells you how many matched. Narrow the date range, tighten the cloud limits, or raise the limit (`--max-scenes 0` removes it). Sentinel-2 revisits every 2–5 days, so a year can easily be 100+ scenes.
- **Failures don't stop the run.** If one scene fails to download, it's reported and the rest continue. Run the same command again with `--resume` to retry only the failed ones.

#### Resuming an interrupted run

Long series can take an hour or more. If a run is interrupted (Ctrl+C, a network outage, a closed laptop), run the **same command again with `--resume`**:

```bash
python download_s2.py my_area.geojson --start 2015-06-01 --end 2026-10-14 --all-scenes --max-cloud 30 --max-aoi-cloud 5 --max-scenes 0 --resume
```

- Scenes whose reflectance GeoTIFF is already in `--out-dir` are **skipped**; only the missing ones are downloaded.
- With `--max-aoi-cloud`, the AOI cloud of already-downloaded scenes is read from their files (it is stored in the GeoTIFF metadata), so they are **not re-checked**.
- `--max-scenes` counts only the scenes **still to download**.
- The scene list CSV is rewritten and lists **every** scene, old and new.
- Interrupted downloads are safe to resume. The reflectance file is written as `….part.tif` and renamed only once the scene is complete, so a half-written scene is never mistaken for a finished one. Leftover `.part.tif` files are overwritten on the next run.

Use the **same `--out-dir`, `--name` and `--bands`** as the original run, because skipping works by file name. Don't reuse an output folder for a different area under the same file name: those scenes would be skipped too. `--resume` also works without `--all-scenes`: it skips the download if the best scene is already there.

Combining `--all-scenes` with `--max-aoi-cloud` is recommended. It keeps cloudy dates out of the series without throwing away dates that are clear over your area.

### Choosing bands

Use `--bands` to choose what to download. The default is `rgb`. You can pass either a **preset** or a **comma-separated list of bands**:

```bash
python download_s2.py my_area.geojson --bands falsecolor        # a preset
python download_s2.py my_area.geojson --bands nir,red,green     # band names...
python download_s2.py my_area.geojson --bands B08,B04,B03       # ...or band IDs (B8 also works)
python download_s2.py my_area.geojson --bands all               # all 12 spectral bands
python download_s2.py --list-bands                              # show every band and preset
```

The bands are written to the output **in the order you list them**, which matters for colour composites: the first band is shown as red, the second as green and the third as blue.

**Presets:**

| Preset | Bands (R, G, B) | Shows |
|---|---|---|
| `rgb` *(default)* | B04, B03, B02 | True colour, as the eye sees it |
| `falsecolor` | B08, B04, B03 | Colour infrared: healthy vegetation shows bright red, so it stands out from cities and bare ground |
| `swir` | B12, B08, B04 | Built-up areas, bare soil, geology and burn scars |
| `agriculture` | B11, B08, B02 | Crop health and moisture |
| `all` | B01–B12 | All 12 spectral bands in one GeoTIFF, for analysis (no preview image) |

**Available bands:**

| Name | Band | Wavelength | Native resolution | Typical use |
|---|---|---|---|---|
| `coastal` | B01 | 443 nm | 60 m | Aerosols, coastal water |
| `blue` | B02 | 490 nm | 10 m | True colour, water |
| `green` | B03 | 560 nm | 10 m | True colour, vegetation |
| `red` | B04 | 665 nm | 10 m | True colour, chlorophyll absorption |
| `rededge1` | B05 | 704 nm | 20 m | Vegetation stress |
| `rededge2` | B06 | 740 nm | 20 m | Vegetation, leaf area |
| `rededge3` | B07 | 783 nm | 20 m | Vegetation, leaf area |
| `nir` | B08 | 842 nm | 10 m | Vegetation (NDVI), water/land boundaries |
| `nir08` | B8A | 865 nm | 20 m | Narrow NIR, vegetation, moisture |
| `nir09` | B09 | 945 nm | 60 m | Water vapour |
| `swir16` | B11 | 1610 nm | 20 m | Moisture, built-up areas (NDBI), snow vs cloud |
| `swir22` | B12 | 2190 nm | 20 m | Geology, burn severity, built-up areas |

B10 (cirrus) is used only during processing and is not included in Level-2A data.

**Resolution:** all output is on a **10 m grid**. The 20 m and 60 m bands are resampled to 10 m so they line up pixel-for-pixel with the 10 m bands. Resampling does not add detail: a 60 m band still only shows 60 m detail. The default resampling method is `bilinear`; use `--resampling nearest` to keep the original pixel values unchanged (each 20 m pixel becomes 2 × 2 identical 10 m pixels).

### All options

```
python download_s2.py AOI [--bands BANDS] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                          [--max-cloud PERCENT] [--max-aoi-cloud PERCENT]
                          [--all-scenes] [--max-scenes N] [--resampling METHOD]
                          [--out-dir DIR] [--name PREFIX] [--resume] [--no-progress]
python download_s2.py --list-bands
```

| Argument | Default | Description |
|---|---|---|
| `AOI` | *(required)* | GeoJSON file containing the polygon (see [Preparing your area of interest](#preparing-your-area-of-interest)) |
| `--bands` | `rgb` | Preset or comma-separated list of bands (see [Choosing bands](#choosing-bands)) |
| `--list-bands` | | Print all bands and presets, then exit |
| `--start` | 90 days ago | First date to search |
| `--end` | today | Last date to search |
| `--max-cloud` | `10`, or `100` with `--max-aoi-cloud` | Maximum cloud cover of the **whole tile**, in percent (catalogue metadata) |
| `--max-aoi-cloud` | not checked | Maximum cloud cover **inside your polygon**, in percent, measured from the SCL layer (see [Filtering clouds over your area](#filtering-clouds-over-your-area)) |
| `--all-scenes` | off | Download every matching scene (one per date) instead of only the best one (see [Downloading a time series](#downloading-a-time-series)) |
| `--max-scenes` | `50` | With `--all-scenes`, stop without downloading if more scenes than this match; `0` means no limit |
| `--resampling` | `bilinear` | How 20 m and 60 m bands are brought to 10 m: `nearest`, `bilinear` or `cubic` |
| `--out-dir` | `output` | Folder where the results are saved |
| `--name` | AOI file name | Prefix for the output file names |
| `--resume` | off | Skip scenes already downloaded to `--out-dir`, to continue an interrupted run (see [Resuming an interrupted run](#resuming-an-interrupted-run)) |
| `--no-progress` | off | Hide the progress bars, e.g. when saving the output to a log file |

Run `python download_s2.py --help` to see the same list in the terminal.

### Examples

These use the bundled Douma AOI; replace the path with your own file.

```bash
# Least-cloudy true-colour image from summer 2026
python download_s2.py examples/douma/douma.geojson --start 2026-06-01 --end 2026-08-31

# False-colour image to highlight vegetation
python download_s2.py examples/douma/douma.geojson --start 2026-06-01 --end 2026-08-31 --bands falsecolor

# Only the bands needed for NDVI (red and NIR)
python download_s2.py examples/douma/douma.geojson --bands red,nir

# Every spectral band, keeping original pixel values
python download_s2.py examples/douma/douma.geojson --bands all --resampling nearest

# Cloudier season: judge clouds over the AOI, not the whole tile
python download_s2.py examples/douma/douma.geojson --start 2025-12-01 --end 2026-02-28 --max-aoi-cloud 5

# Time series: every clear scene of 2026 in false colour, in its own folder
python download_s2.py examples/douma/douma.geojson --start 2026-01-01 --end 2026-09-30 --all-scenes --max-aoi-cloud 5 --bands falsecolor --out-dir output/douma_2026

# Historical image, saved to its own folder with a custom file prefix
python download_s2.py examples/douma/douma.geojson --start 2018-03-01 --end 2018-05-31 --out-dir output/2018 --name douma_2018
```

### Progress output

Each step of the process shows a progress bar, so you can see what the script is doing and roughly how long is left:

| Step | Bar | Counts |
|---|---|---|
| Catalogue search | `Searching catalogue` | Scenes returned by the catalogue; the label shows how many fully cover the AOI |
| AOI cloud check (with `--max-aoi-cloud`) | `Checking AOI cloud` | Scenes checked; the label shows how many are clear. One line per scene is printed above the bar. |
| Download of one scene | `Downloading <date>` | Steps: output grid, **each band**, writing the reflectance GeoTIFF, writing the previews. The label names the current step, for example `nir (B08)`. |
| Time series (with `--all-scenes`) | `Scenes` | Scenes downloaded. Each scene's own `Downloading <date>` bar appears below it while it runs, and a `saved …` line is printed when it finishes. |

Example, single scene:

```
Searching catalogue: 100%|██████████| 67/67 [00:03<00:00, 18.50scene/s, 34 cover the AOI]
Found 34 scenes (one per date) fully covering the AOI between 2026-06-01 and 2026-09-30 with tile cloud < 10%.
Best scenes:
  S2A_37SBT_20260913_1_L2A  2026-09-13  tile cloud=0.00%
  S2B_37SBT_20260916_0_L2A  2026-09-16  tile cloud=0.00%
  ...
Downloading S2A_37SBT_20260913_1_L2A  2026-09-13  tile cloud=0.00%
  3 bands on a 560x505 px 10 m grid
Downloading 2026-09-13: 100%|██████████| 6/6 [00:13<00:00,  2.19s/step, done]
Saved:
  output\douma_20260913_rgb_reflectance.tif
  output\douma_20260913_rgb_8bit.tif
  output\douma_20260913_rgb.png
```

Example, time series (while scene 2 of 3 is downloading):

```
  saved S2C_37SBT_20260901_0_L2A  2026-09-01  tile cloud=0.68%
Scenes:  33%|███▎      | 1/3 [00:13<00:26, 13.14s/scene]
Downloading 2026-09-03:  50%|█████     | 3/6 [00:06<00:07,  2.45s/step, blue (B02)]
```

The bars are drawn on the terminal's error stream (stderr), and the results on standard output (stdout). When you save the output to a file (`python download_s2.py ... > log.txt`), only the results go to the file. Use `--no-progress` to turn the bars off completely.

---

## Outputs

Files are named `<name>_<YYYYMMDD>_<bands>_...`, where:
- `<YYYYMMDD>` is the date the scene was acquired;
- `<bands>` is the preset name (`rgb`, `falsecolor`, `all`, …) or the band names joined with `-` (for example `swir22-nir-red`).

| File | Format | Use |
|---|---|---|
| `*_reflectance.tif` | GeoTIFF, one band per requested band, **uint16** | Analysis. Surface reflectance × 10000; divide by 10000 to get reflectance from 0 to 1. Each band is labelled with its ID and wavelength (for example `B08 NIR 842 nm`). |
| `*_8bit.tif` | GeoTIFF, **uint8** | Viewing in QGIS/ArcGIS or as a basemap, with a contrast stretch applied. |
| `*.png` | PNG | Quick visual check. Not georeferenced. |
| `<name>_scenes.csv` | CSV | Only with `--all-scenes`: one row per downloaded scene (date, STAC item, tile cloud %, AOI cloud %, reflectance file). |

The `_8bit.tif` and `.png` previews are made only when you request **3 bands** (a colour composite) or **1 band** (greyscale). For other counts, such as `--bands all`, only the reflectance GeoTIFF is written.

**Contrast stretch for the previews:**
- `rgb` uses a fixed stretch of 0–0.30 reflectance, so true-colour images from different dates can be compared directly.
- All other selections stretch each band to its own 98th percentile inside the AOI, because NIR and SWIR bands are much brighter than visible ones.

Common to both GeoTIFFs:
- **Projection:** the UTM zone of the Sentinel-2 tile (WGS 84 / UTM, e.g. EPSG:32637 for zone 37N). The data is not reprojected.
- **Resolution:** 10 m for every band (see [Choosing bands](#choosing-bands))
- **NoData:** `0` (pixels outside the polygon)
- **Compression:** Deflate, tiled
- **Metadata tags:** the STAC item ID, tile cloud cover, AOI cloud cover (when checked) and band list are stored in the reflectance file (`gdalinfo` shows them)

---

## How it works

1. **Load the AOI** from the GeoJSON file.
2. **Search** the Earth Search `sentinel-2-l2a` collection for scenes that intersect the AOI within the date range and below the tile cloud limit.
3. **Filter** out scenes whose footprint does not fully contain the AOI, such as scenes cut off at the edge of a satellite pass, and keep one scene per date.
4. **Check AOI cloud** (only with `--max-aoi-cloud`): read each scene's SCL layer inside the polygon, compute the share of cloud, cirrus and cloud-shadow pixels, and drop scenes above the limit.
5. **Choose scenes:**
   - **By default**, rank by cloud cover, using AOI cloud when it was checked and tile cloud otherwise, with the most recent first among ties. The top 5 are printed and the first is downloaded.
   - **With `--all-scenes`**, keep every scene, check the `--max-scenes` limit, and download them oldest first.

   Steps 6 to 9 then run for each scene being downloaded.
6. **Define the output grid:** a 10 m grid covering the AOI, aligned to the pixels of the scene's 10 m bands.
7. **Download** each requested band. The bands are Cloud-Optimized GeoTIFFs, so rasterio reads only the blocks around the AOI over HTTP. Each band is read onto the output grid, which resamples 20 m and 60 m bands to 10 m, and pixels outside the polygon are masked.
8. **Correct the 1000 offset.** Scenes from processing baseline 04.00 onwards (January 2022 and later) store pixel values with 1000 added. The script removes this offset when Earth Search reports it has not already been removed (`earthsearch:boa_offset_applied`), so values from older and newer scenes can be compared.
9. **Write** the reflectance GeoTIFF, then, for 1- or 3-band selections, stretch it to 8 bits with a mild gamma to make the preview GeoTIFF and PNG. With `--all-scenes`, also add a row to the scene list CSV.

**Note on cloud cover:** `--max-cloud` alone filters on the cloud percentage of the **whole Sentinel-2 tile**, which can be misleading for a small AOI. Use `--max-aoi-cloud` for a reliable result. The SCL classification itself is automatic and not perfect: very thin haze can be missed, and bright rooftops or sand are occasionally flagged as cloud.

---

## Adding an example

To contribute an AOI to `examples/`:

1. Create `examples/<place>/` containing `<place>.geojson`.
2. Add a short `README.md` that says where the polygon came from, its approximate size, the data licence and a run command.
3. Optionally include a small preview PNG from a run.

See [`examples/douma/`](examples/douma/) for the layout.

---

## Troubleshooting

**`No scenes match (tile cloud < X% ...)`**
Widen the date range (`--start`/`--end`), raise `--max-cloud`, or switch to `--max-aoi-cloud`, which often finds clear scenes that the tile filter rejects. Sentinel-2 images the same spot about every 5 days, but persistently cloudy regions and seasons need wider windows. If no scene is found even with generous settings, your AOI may cross a tile boundary or be larger than one tile; split it into smaller pieces.

**`N scenes match, more than --max-scenes 50`**
This is the `--all-scenes` safety limit, and nothing has been downloaded yet. Narrow the date range, add `--max-aoi-cloud` to drop cloudy dates, or raise the limit with `--max-scenes 200` (or `0` for no limit) if you really want them all. Things to keep in mind:
- Sentinel-2 data starts in **mid-2015**, so earlier start dates add nothing. The full archive from 2015 is typically 400+ scenes under 10% tile cloud, at about 10–15 seconds per scene.
- When adding `--max-aoi-cloud` to a long range, also keep a loose tile filter such as `--max-cloud 30`. Otherwise every scene since 2015 is cloud-checked before anything downloads.
- For multi-hour runs, add `--resume` so an interruption doesn't mean starting over (see [Resuming an interrupted run](#resuming-an-interrupted-run)).

**`AOI cloud check failed ...; skipped`**
The SCL layer of that scene couldn't be read, usually because of a temporary network error. The scene is left out; run the command again to include it.

**`CRSError: The EPSG code is unknown ... proj.db ... comes from another PROJ installation`**
Another program, such as PostGIS, QGIS or OSGeo4W, has set a `PROJ_LIB` environment variable that points to an incompatible PROJ database. The script already works around this by using rasterio's bundled PROJ data. If you see this error in your own code, run `set PROJ_LIB=` (cmd) or `Remove-Item Env:PROJ_LIB` (PowerShell) before running it.

**`ModuleNotFoundError: No module named 'rasterio'`** (or another package)
The virtual environment isn't active. Activate it (see [Installation](#installation)) or run the script with its Python directly:
`.venv\Scripts\python download_s2.py my_area.geojson` (Windows) or `.venv/bin/python download_s2.py my_area.geojson` (Linux/macOS).

**`KeyError: 'features'` or other errors when reading the AOI**
Make sure the file is valid GeoJSON in EPSG:4326 (lon/lat in degrees, not metres). Check it by opening it at [geojson.io](https://geojson.io).

**Timeouts or HTTP errors while downloading**
The STAC API or S3 may be temporarily unavailable. Wait a minute and run the script again.

**The image looks too dark or washed out**
For `rgb`, the 8-bit stretch is fixed at 0–0.30 reflectance, which suits most land surfaces; very bright areas, such as desert or snow, may need a higher limit. Change the `high=0.30` value where `to_8bit()` is called in `download_s2.py` (or `gamma` in `to_8bit()` itself), or load the uint16 reflectance GeoTIFF in QGIS and style it there. Other band selections are stretched automatically per band.

**`Unknown band 'xyz'`**
Run `python download_s2.py --list-bands` for the accepted names. Band names (`nir`), band IDs (`B08` or `B8`) and preset names (`falsecolor`) are all accepted, in any letter case. Separate bands with commas and no spaces, or put the whole list in quotes.

---

## Data sources and credits

- **Imagery:** Copernicus Sentinel-2 data, processed by ESA, Level-2A. When you publish images, credit them as *"Contains modified Copernicus Sentinel data [year]"*.
- **Catalogue and hosting:** [Element 84 Earth Search](https://element84.com/earth-search/) and the [AWS Open Data Registry](https://registry.opendata.aws/sentinel-2-l2a-cogs/).
- **Example AOIs:** see the README in each example folder for its source and licence.
