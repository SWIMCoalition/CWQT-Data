# CWQT Data Portal

The **Community Water Quality Testing Data Portal**: an interactive map and charts of Enterococcus
results from the CWQT program, based on the CWQT master Google Sheet.

**Data Portal:** <https://SWIMCoalition.github.io/CWQT-Data/>

**More about CWQT:** <https://www.swimmablenyc.org/cwqt>

Results entered in the master sheet automatically appear on the map the next morning.

---

## What's in this repository

| File | Purpose |
|---|---|
| `cwqt_visualize.py` | Reads the workbook, calculates the statistics and writes every output. All thresholds and definitions are in its **CONFIGURATION** section near the top. |
| `templates/` | The web pages' design: `map.html` (map page), `charts.html` (charts page), `cwqt.css` (shared colors, fonts, banner). Edit these to change how the portal looks; see [Changing the look](#changing-the-look-of-the-portal). |
| `templates/assets/` | SWIM branding: `swim-logo.png` (banner and chart footers), `swim-emblem.png` (browser-tab icon) and `fonts/` (Poppins, used in the charts; free under the SIL Open Font License, see `OFL-LICENSE.txt`). |
| `data/` | Extra map layers (partner labs, outfalls, drainage areas, green infrastructure) and `layers.json`, which says how each one is shown, plus `CWQT_Sites_GoogleMyMap.csv`, which supplies each site's "See raw data" link. See [Map layers](#map-layers). |
| `requirements.txt` | Python packages the script needs. |
| `.github/workflows/update-map.yml` | The daily job: download the sheet → run the script → publish the site. |
| `status.json` | Written by the daily job: latest sample date, sample count and site count. A new commit means new data arrived. |
| `.gitignore` | Keeps the workbook and generated files out of the repository. |

## How the numbers are calculated

Each run, the script recalculates the **30 Day GeoMean Primary Contact** tab and prints whether 
every site matches.

- **Censored results:** `<10` counts as 10, `>24196` as 24196 and `>6000` as 6000.
- **Geometric mean** = exp(mean(ln(MPN))).
- **Categories** (NYC DOH primary contact scale):
  - Good: geomean ≤ 35
  - Advisory: 35–104
  - Poor: > 104
- **Single-sample limit** for "% of samples over": 104.
- **Fewer than 5 samples:** a geomean from fewer than 5 samples is shown with a dashed / hollow
  marker, because standards require at least 5.
- **NYS DEC criteria** shown in map popups:
  - Class I: geomean 72, threshold 266
  - Classes SB and SA: geomean 35, threshold 130
- **Regular season** = May 1 – Oct 5. Seasonal statistics use only these samples. Winter and
  post-season samples appear in the "Latest 30 days" layer but not in seasonal geomeans.
- **Wet weather** = at least 0.25" of rain at Central Park on the sampling day plus the 2 days
  before. Precipitation text is cleaned up first: "Trace" counts as 0, and "0.45 (3.2 snow)" counts
  as 0.45.
- **Lab/field splits** recorded in Notes are ignored as of now, so the numbers match the GeoMean
  tabs. Run with `--average-splits` to average them as the FAQ describes.

### What the script expects in the sheet
Columns are found **by their header names**, so moving or inserting columns is fine.
- **Required.** Renaming any of these stops the daily run:
  - Data tab: `Site`, `Full Date` and
    `Most Probable Number (MPN) of Enterococcus colonies per 100 ml`.
  - Information tab: a header row containing `Table of Contents - Site (Listed Alphabetically)`
    followed by `Site ID`, plus `Currently Testing`, `Waterbody`, `Borough/State`,
    `Neighborhood/Town`, `Latitude`, `Longitude` and `NYS DEC Water Body Classification`.
- **Used if present.** Renaming one of these quietly turns off the feature that depends on it:
  - `Site ID` (otherwise sites are matched by name);
  - `Notes` (lab splits and QC notes);
  - the seven precipitation columns, `Day of Collection Precipitation (Thursday)`,
    `Previous Day Precipitation (Wednesday)`, `Previous Tuesday Precipitation`, … (wet/dry weather).

New sites only need a row on the Information tab (with coordinates) and a matching Site ID in Data.

---

## The daily update

`.github/workflows/update-map.yml` runs every day at 11:17 UTC (7:17 am EDT / 6:17 am EST) on
Ubuntu 24.04:

1. Downloads the sheet as .xlsx, using `CWQT_SHEET_ID`.
2. Runs `python cwqt_visualize.py --out build`.
3. Copies the public files to `site/` and deploys them to GitHub Pages.
4. Commits `status.json` if the data changed. If 45 days pass with no commit (e.g. off-season), it
   makes an empty "keep-alive" commit instead, because GitHub pauses scheduled jobs in repositories
   with no commits for 60 days.

**Update now:** Actions → *Update CWQT map* → **Run workflow**.

**Check it's working:**
- The map title says "Data through <date>".
- The Actions tab shows a green check for each morning.
- The commit history shows "Data updated: …" after new results are entered.

### If a run fails
GitHub emails the person who set up the workflow (or who last changed its schedule line). Open the failed run → `update` job to see the
error, then check:

| Message | Likely cause / fix |
|---|---|
| "Could not download the Google Sheet" / "Google returned a web page" | The sheet's sharing changed, the sheet ID secret is wrong, or the service-account key was deleted. |
| "Data tab is missing expected column(s)" / "Could not find a header row" | A column or tab in the sheet was renamed. Rename it back, or update the names in the CONFIGURATION section. |
| Fails in "deploy" | Settings → Pages → Source must be **GitHub Actions**. |
| No runs at all for weeks | The schedule was paused. Open Actions → *Update CWQT map* → **Enable workflow**. |

GitHub occasionally retires old versions of the actions used in the workflow (`actions/checkout`,
`setup-python`, …). Warnings about this appear as "annotations" on a run. Updating the version
numbers in the `uses:` lines fixes them.

---

## Changing the look of the portal

The design lives in `templates/` and is separate from the calculations. Edit those files on GitHub
(pencil icon → commit), then run the workflow (or wait for the next morning) to publish.

| To change… | Edit |
|---|---|
| Colors, fonts, button style, banner height | the **SWIM BRAND** block at the top of `templates/cwqt.css` |
| Banner title, button labels, popup wording, legend text | `templates/map.html` |
| How popup titles are written ("Rutgers Boathouse, Raritan River" / "New Brunswick · New Jersey") | `popup_heading` in `cwqt_visualize.py`. The titles come from the Site name, Waterbody, Borough/State and Neighborhood/Town columns on the Information tab. |
| The "See raw data" links in site popups | the `results_sheet_url` column of `data/CWQT_Sites_GoogleMyMap.csv`, matched to sites by `site_id`. To use a different file or column, change `SITE_LINKS_FILE` / `SITE_LINKS_COLUMN` in `cwqt_visualize.py`. Sites without a link simply don't show one. |
| The small time-series chart in site popups | `sparkline` in `templates/map.html` |
| Chart descriptions and page text on the charts page | `templates/charts.html` |
| The logo | Replace `templates/assets/swim-logo.png`. The script trims empty transparent margins automatically, so any version of the logo works. It's used in the banner and in every chart's footer. |
| The browser-tab icon (favicon) | Replace `templates/assets/swim-emblem.png`, ideally a square PNG with a transparent background, 512 px or larger. The script makes all the sizes browsers need (`favicon.ico`, `favicon-32.png`, `apple-touch-icon.png`), so there's nothing to convert. |
| Chart fonts and colors | `templates/assets/fonts/` (Poppins), plus `INK`, `SERIES`, etc. in the CONFIGURATION section of `cwqt_visualize.py`. The footer text on each chart is `CHART_FOOTER`. |
| Basemap choices or the streams overlay | the `BASEMAPS` and `WATERWAYS` lists near the top of the script in `templates/map.html`. `WATERWAYS.minZoom` sets how far in you must zoom before streams appear. |
| Which waterbodies count as creeks, bays or major rivers in the "recovery after rain" chart | `WATERBODY_GROUPS` and `SITE_GROUP_OVERRIDES` in the CONFIGURATION section of `cwqt_visualize.py` |
| Thresholds, categories, season dates | the CONFIGURATION section of `cwqt_visualize.py` (used by the charts *and* the map) |

Words in double curly braces in the templates (e.g. `{{DATA}}`, `{{N_SITES}}`) are filled in by the
script. Keep them where you want those values to appear.

**Current design** (November 2026), matched to swimmablenyc.org:
- Poppins font, SWIM navy `#092e6e` with square-cornered buttons, SWIM blue `#0d4ab2` links, cream
  `#f7f7f3` page background.
- Light gray basemap with the USGS streams and rivers overlay on top, so narrow waterways like the
  Bronx River stand out. The overlay appears from the ~1-mile zoom level, because USGS labels every
  lake and reservoir at the citywide view. "Streets" and "Satellite" basemaps are also available.
- On phones the legend starts collapsed, button labels shorten, and tap targets are larger.
- Charts use Poppins and SWIM navy, with a footer showing the source, the "Data through" date and the
  SWIM logo. The charts page ends with the SWIM emblem.
- Site popups: the title and place, the current category, the geometric mean and most recent result,
  a small chart of every season's geomean (points colored good / advisory / poor), then
  "See full time series" (the site's PDF) and "See raw data" (the site's results sheet).
- Charts page order: (1) water quality over time, (2) recent water quality by waterbody,
  (3) rainfall, (4) recovery after rain, (5) the latest 30 days by waterbody, (6) every site, every
  season, (7) each site's full time series.
- Each site's PDF has two pages: (1) its seasonal geomean by year, colored by category, for a quick
  read; (2) every sample, with the rolling 30-day geomean.

---

## Map layers

Besides the CWQT sites, the map's Layers menu offers the datasets in `data/`:

| Group | Layer | Shown from | Style |
|---|---|---|---|
| Partner Laboratories | Active Labs (on when the map opens) | any zoom | SWIM blue flask (`status` is "Active") |
| Partner Laboratories | Historic Labs | any zoom | gray flask (any other `status`) |
| Sewers & Stormwater | Outfalls | ~1-mile zoom (12) | colored by type: CSO, MS4, highway, direct drainage, other |
| Sewers & Stormwater | Combined sewer drainage areas | any zoom | violet outlines |
| Green Infrastructure | Green infrastructure sites | ~1-mile zoom (12) | green dots |
| Green Infrastructure | Porous pavement, Medians, Regulated projects | 12 | green shapes |

The green infrastructure layers appear at the same zoom as the streams overlay. Each layer except Active Labs is off
until someone checks it, and is only downloaded then. Hovering over a lab shows its name; clicking
any feature shows its details (for labs, one row per contact, from `lab_contacts`, which is written
as `Lab Manager: Name; Lab Tech: Name`). Point layers only draw what's on screen, so even the 16,000
green infrastructure sites stay fast: with every green infrastructure layer on at zoom 12 the map
uses about 21 MB of memory, versus about 4 MB with only the CWQT sites. If that feels slow on
phones, raise their `minZoom` to 13 in `data/layers.json`.

**How it works.** `data/layers.json` lists each layer: which file(s), which fields go in the popup,
colors, and the zoom level it appears at. On each run the script reads those files and writes small
web-ready copies to `layers/` on the site, keeping only the listed fields, rounding coordinates to
about 1 m and simplifying outlines. The ~24 MB of raw downloads become about 1.8 MB.

**Updating a dataset.** Replace the file in `data/` with the new download. File names in
`layers.json` use `*` (e.g. `NYC_Citywide_Outfalls_*.json`), so a newly dated file is picked up
automatically. If two files match, the newest name wins, so delete the old one.

**Adding a dataset.** Put the file in `data/`, then copy an existing entry in `data/layers.json` and
edit it. The notes at the top of that file explain each setting, including `where` (split one file
into several layers, as the Active/Historic labs do) and `style.icon` (draw an icon instead of a dot). Files must be GeoJSON in
latitude/longitude, like NYC Open Data's GeoJSON exports. Shapefiles and KML files need converting
first, e.g. at <https://mapshaper.org>: drop in the file (for a shapefile, all of its parts: `.shp`,
`.shx`, `.dbf` and `.prj`), then Export → GeoJSON. While there:
- Make sure it's in latitude/longitude. NYC data often comes in State Plane feet; in mapshaper, type
  `-proj wgs84` in the console.
- No need to simplify; the script does that.

---

## Running it on your own computer

```bash
pip install -r requirements.txt

# from a downloaded copy (Google Sheets → File → Download → Microsoft Excel)
python cwqt_visualize.py CWQT_Master_Data_Sheet.xlsx

# or straight from Google Sheets
python cwqt_visualize.py --sheet-id <long code from the sheet's URL>
```

Options:

| Option | Effect |
|---|---|
| `--out FOLDER` | Where outputs go (default `cwqt_outputs/`). |
| `--window-end YYYY-MM-DD` | End date of the 30-day window (default: latest sample). |
| `--average-splits` | Average lab/field split results recorded in Notes. |
| `--site-pngs` | Also save each site's two charts as PNGs (`…_geomean.png` and `…_samples.png`). |
| `--no-map` | Skip the web pages (map and charts page). |

Outputs go to `cwqt_outputs/` (or the `--out` folder). Open `index.html` there in a browser to
preview the portal before publishing.

---

## Known data issues to fix in the sheet
Run the script locally and open `data_quality_report.txt` for the current list. Found in October 2026:
- 8 duplicate site+date rows.
- 5 rows with no MPN result.
- 4 site names in Data that differ from Information.

Found in November 2026 in the `data/` files:
- `CWQT_Sites_GoogleMyMap.csv` has no `results_sheet_url` for sites 6 and 157–160, so those sites have
  no "See raw data" link yet. It also lists site IDs 3, 4 and 11, which aren't on the map, and 17 site
  names that differ from the master sheet (harmless: links are matched by ID).
- `CWQT_Partner_Labs.geojson` has 3 labs with no `lab_contacts` (their popups say "No lab contacts
  listed").
- Battery High Tide: five dates have times that don't fit the 12.4-hour tide cycle (four look like
  low-tide times): 2023-08-31, 2026-07-16, 2026-07-23, 2026-07-30 and 2026-08-06.

Fixed in October 2026: site 160's (Hackensack) positive longitude, and the 2026 rainfall, which is now
entered, so 2026 has a wet/dry split. The script still checks for both, in case they recur.
