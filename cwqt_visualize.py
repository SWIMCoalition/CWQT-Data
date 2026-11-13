#!/usr/bin/env python3
"""
CWQT (Community Water Quality Testing) maps & charts
=====================================================

Reads the CWQT master data workbook (downloaded from Google Sheets as .xlsx)
and produces maps and charts of Enterococcus results.

    python cwqt_visualize.py CWQT_Master_Data_Sheet.xlsx
    python cwqt_visualize.py CWQT_Master_Data_Sheet.xlsx --out figures --window-end 2026-08-31
    python cwqt_visualize.py CWQT_Master_Data_Sheet.xlsx --site-pngs      # also each site's charts as PNGs
    python cwqt_visualize.py --sheet-id 1AbC...xyz     # download straight from Google Sheets

Download the sheet from Google Sheets with File > Download > Microsoft Excel (.xlsx).
The web pages' look and wording live in templates/ (map.html, charts.html, cwqt.css);
keep that folder next to this script.
Thresholds, the wet-weather definition and the season dates are in the CONFIGURATION
section below.

Outputs (written to ./cwqt_outputs by default)
----------------------------------------------
  index.html                         Interactive map (the "Data Portal"): season layers, basemaps,
                                     extra layers from data/ (partner labs, outfalls, green
                                     infrastructure), popups with a mini trend chart, a link to the
                                     site's PDF and a "See raw data" link.
  layers/*.json                      Web-ready copies of the extra map layers.
  charts.html                        Page listing every chart with a short description.
  cwqt.css                           Shared look for both pages (copied from templates/).
  1_program_trend.png                Program-wide water quality by season, wet vs dry weather
  2_waterbody_comparison.png         Recent geomean by waterbody, one dot per site
  3_rainfall_effect.png              How results change with rainfall in the prior ~72 hours
  4_rain_recovery.png                How bacteria fall in the days after rain, by kind of waterbody
  5_green_infrastructure.png         Green infrastructure built per year (running total), the pipeline,
                                     and the most common types (from the DEP files in data/)
  6_latest_30_days_by_waterbody.png  Latest 30-day geomean by waterbody, one dot per site
  7_site_by_season_heatmap.png       Every site x every season, colored by seasonal geomean
  site_timeseries.pdf                Every site, two pages each: (1) seasonal geomean by year,
                                     (2) every regular-season sample, by season
  sites/site-NNN.pdf                 The same two pages for each site on its own (linked from the map)
  tables/*.csv                       The numbers behind every chart (open in Sheets/Excel)
  data_quality_report.txt            Things in the workbook worth fixing

Requirements:  pip install pandas openpyxl matplotlib
               (plus google-auth requests only if downloading a private sheet)

How values are treated (matches the workbook FAQ and the GeoMean tabs)
----------------------------------------------------------------------
  * "<10"     -> 10      (non-detect, shown as the detection limit)
  * ">24196"  -> 24196   (above IDEXX upper limit with 10 ml dilution)
  * ">6000"   -> 6000    (same idea, other dilution)
  * "Trace" precipitation -> 0.0 in;  "0.45 (3.2 snow)" -> 0.45 in
  * Geometric mean = exp(mean(ln(MPN)))
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")  # draw to files, no window needed
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.colors import ListedColormap  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402
from matplotlib.transforms import offset_copy  # noqa: E402

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# =============================================================================
# CONFIGURATION: edit these to change thresholds, colors or definitions
# =============================================================================

# Sheet / column names in the workbook. Columns are found by these names, so
# moving or inserting columns is fine; renaming them is not.
DATA_SHEET = "Data"
INFO_SHEET = "Information"
ALT_NAMES_SHEET = "Site Names + Site IDs"

COL_SITE = "Site"
COL_SITE_ID = "Site ID"
COL_DATE = "Full Date"
COL_TIME = "Sample Time"
COL_MPN = "Most Probable Number (MPN) of Enterococcus colonies per 100 ml"
COL_NOTES = "Notes"
# Daily rainfall columns (inches, Central Park), most recent day first.
# "Wet weather" = sum of the first RAIN_DAYS of these >= WET_WEATHER_INCHES.
RAIN_COLS = [
    "Day of Collection Precipitation (Thursday)",
    "Previous Day Precipitation (Wednesday)",
    "Previous Tuesday Precipitation",
    "Previous Monday Precipitation",
    "Previous Sunday Precipitation",
    "Previous Saturday Precipitation",
    "Previous Friday Precipitation",
]
RAIN_DAYS = 3                # day of sampling + 2 previous days (~72 hours)
WET_WEATHER_INCHES = 0.25

# Water quality categories (NYC DOH primary contact, the same green/yellow/red
# scheme used on the "30 Day GeoMean Primary Contact" tab).
GOOD_MAX = 35                # geomean <= 35  -> acceptable for primary contact
ADVISORY_MAX = 104           # 35-104 -> advisory;  > 104 -> closure-level
SINGLE_SAMPLE_LIMIT = 104    # single-sample threshold used for "% of samples over"
MIN_SAMPLES_FOR_GEOMEAN = 5  # standards call for >= 5 samples in a 30-day geomean

# NYS DEC class-specific criteria (geomean, statistical threshold value), shown
# in map popups. Class I numbers are DEC's 72/266 criteria used on the Class I tab.
DEC_CRITERIA = {"SB": (35, 130), "SA": (35, 130), "I": (72, 266)}

# How many recent seasons to use for "recent average" charts (waterbody chart).
RECENT_SEASONS = 5

# Regular sampling season (month, day), inclusive. Seasonal statistics use only
# samples in this window; winter / post-season samples are kept out of them.
# (The first Thursday of October has usually been the last regular-season test.)
SEASON_START = (5, 1)
SEASON_END = (10, 5)

# Colors. Status colors are reserved for good / advisory / poor and are always
# paired with a text label. SERIES is for ordinary data marks.
COLOR_GOOD = "#0ca30c"
COLOR_ADVISORY = "#fab219"
COLOR_POOR = "#d03b3b"
COLOR_NODATA = "#c3c2b7"
SERIES = "#0d4ab2"          # SWIM blue - main series
SERIES_2 = "#eb6834"        # orange    - second series (wet weather)
INK = "#092e6e"             # SWIM navy - titles and text
INK_2 = "#4f5d78"
MUTED = "#8a93a6"
GRID = "#e4e7ee"
SURFACE = "#ffffff"

# Kinds of waterbody, used by the "recovery after rain" chart. Waterbodies not listed count as
# "Major rivers & harbor"; SITE_GROUP_OVERRIDES moves single sites (enclosed basins, inlets).
WATERBODY_GROUPS = {
    "Creeks, canals & basins": ["Newtown Creek", "Gowanus Canal", "Coney Island Creek", "Flushing Creek",
                                "Bronx River", "Bronx Kill", "Hutchinson River", "Saw Mill River", "Rahway River"],
    "Bays & coves": ["Flushing Bay", "Little Neck Bay", "Jamaica Bay", "Sheepshead Bay"],
    "Lakes": ["Prospect Park Lake", "Meadow Lake", "Kissena Lake", "Willow Lake", "Van Cortlandt Park Lake"],
}
SITE_GROUP_OVERRIDES = {2: "Creeks, canals & basins",    # Wallabout Channel
                        15: "Creeks, canals & basins",   # Anable Basin
                        55: "Creeks, canals & basins",   # Bushwick Inlet
                        74: "Creeks, canals & basins",   # Bush Terminal inner lagoon
                        88: "Creeks, canals & basins",   # Hammond Creek
                        27: "Creeks, canals & basins",   # Paerdegat Basin
                        99: "Creeks, canals & basins"}   # Gerritsen Creek
GROUP_COLORS = {"Major rivers & harbor": "#0d4ab2", "Creeks, canals & basins": "#eb6834",
                "Bays & coves": "#2f9e8f"}               # checked for color-blind separation

# Green infrastructure chart. It reads the files, status rule and construction dates of this map
# layer in data/layers.json, and sorts asset types with the green-grey list there (everything
# else counts as vegetated), so the chart always matches the map.
GI_LAYER = "gi_vegetated"
GI_GREEN_GREY_LIST = "green_grey_types"
GI_COLORS = {"Vegetated": "#2b8a3e", "Green-grey": "#9aa892"}      # same as the map; checked for
GI_BORDERS = {"Vegetated": "#17602a", "Green-grey": "#56654f"}     # color-blind separation
GI_FIRST_YEAR = 2010       # the cumulative bars start here (earlier installations are included)
GI_TOP_TYPES = 6           # the pie shows this many types, plus "Other"

CATEGORY_LABELS = {
    "good": f"Good: ≤ {GOOD_MAX}",
    "advisory": f"Advisory: {GOOD_MAX}–{ADVISORY_MAX}",
    "poor": f"Poor: > {ADVISORY_MAX}",
    "nodata": "No data",
}
CATEGORY_COLORS = {"good": COLOR_GOOD, "advisory": COLOR_ADVISORY,
                   "poor": COLOR_POOR, "nodata": COLOR_NODATA}


# =============================================================================
# LOADING & CLEANING
# =============================================================================

@dataclass
class QAIssue:
    category: str
    detail: str


def _norm(s) -> str:
    """Normalize a site name for matching (case, spaces, punctuation)."""
    s = str(s).lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_mpn(v):
    """Return (numeric_value, censor_flag) for one MPN cell.

    censor_flag is '<' for non-detects, '>' for above-range, '' otherwise.
    """
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return np.nan, ""
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v), ""
    s = str(v).strip().replace(",", "")
    m = re.fullmatch(r"([<>])?\s*(\d+(?:\.\d+)?)", s)
    if not m:
        return np.nan, "?"
    return float(m.group(2)), (m.group(1) or "")


def parse_rain(v) -> float:
    """'Trace' -> 0, '0.45 (3.2 snow)' -> 0.45, numbers pass through."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return np.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)
    s = str(v).strip().lower()
    if s.startswith("trace") or s in {"t", "tr"}:
        return 0.0
    m = re.match(r"(\d*\.?\d+)", s)
    return float(m.group(1)) if m else np.nan


def _find_header_row(raw: pd.DataFrame, must_contain: str) -> int:
    for i in range(min(len(raw), 40)):
        if any(str(x).strip() == must_contain for x in raw.iloc[i].tolist()):
            return i
    raise ValueError(f"Could not find a header row containing '{must_contain}'")


def load_sites(xlsx: Path, qa: list[QAIssue]) -> pd.DataFrame:
    """Site table from the Information tab (one row per site, with coordinates)."""
    raw = pd.read_excel(xlsx, sheet_name=INFO_SHEET, header=None)
    hdr = _find_header_row(raw, "Latitude")
    cols = [str(c).strip() for c in raw.iloc[hdr].tolist()]
    body = raw.iloc[hdr + 1:].copy()
    body.columns = cols

    # The tab has two "Site ID" columns; use the one immediately after the name.
    name_idx = next(i for i, c in enumerate(cols) if c.startswith("Table of Contents - Site"))
    sites = pd.DataFrame({
        "site": body.iloc[:, name_idx],
        "site_id": body.iloc[:, name_idx + 1],
    })
    keep = {
        "Currently Testing": "currently_testing",
        "Waterbody": "waterbody",
        "Borough/State": "borough",
        "Neighborhood/Town": "neighborhood",
        "Latitude": "lat",
        "Longitude": "lon",
        "NYS DEC Water Body Classification": "dec_class",
    }
    for src, dst in keep.items():
        idx = cols.index(src)
        sites[dst] = body.iloc[:, idx].values
    sites = sites.dropna(subset=["site"])
    sites = sites[pd.to_numeric(sites["site_id"], errors="coerce").notna()].copy()
    sites["site_id"] = sites["site_id"].astype(int)
    sites["site"] = sites["site"].astype(str).str.strip()
    sites["lat"] = pd.to_numeric(sites["lat"], errors="coerce")
    sites["lon"] = pd.to_numeric(sites["lon"], errors="coerce")
    sites["currently_testing"] = sites["currently_testing"].astype(str).str.strip().str.lower().eq("x")
    sites["dec_class"] = sites["dec_class"].fillna("").astype(str).str.strip()
    sites["waterbody"] = sites["waterbody"].fillna("Unknown").astype(str).str.strip()

    # Coordinate sanity checks (NYC area: lat ~40.4-41.2, lon ~ -74.6 to -73.6)
    for _, r in sites.iterrows():
        if pd.notna(r.lon) and r.lon > 0:
            qa.append(QAIssue("Coordinates",
                f"Site {r.site_id} '{r.site}': longitude {r.lon} is positive "
                f"(missing minus sign). Using {-r.lon} for maps."))
        if pd.isna(r.lat) or pd.isna(r.lon):
            qa.append(QAIssue("Coordinates", f"Site {r.site_id} '{r.site}' has no coordinates."))
    sites.loc[sites["lon"] > 0, "lon"] *= -1

    dups = sites[sites.site_id.duplicated(keep=False)]
    for sid, g in dups.groupby("site_id"):
        qa.append(QAIssue("Site IDs", f"Site ID {sid} is used for: {'; '.join(g.site)}"))
    return sites.drop_duplicates("site_id").reset_index(drop=True)


def _name_to_id_lookup(xlsx: Path, sites: pd.DataFrame) -> dict[str, int]:
    lookup = {_norm(n): i for n, i in zip(sites.site, sites.site_id)}
    try:
        alt = pd.read_excel(xlsx, sheet_name=ALT_NAMES_SHEET)
        id_col = alt.columns[0]
        for _, r in alt.iterrows():
            if pd.isna(r[id_col]):
                continue
            for c in alt.columns[1:]:
                if pd.notna(r[c]):
                    lookup.setdefault(_norm(r[c]), int(r[id_col]))
    except (ValueError, KeyError):
        pass
    return lookup


def load_samples(xlsx: Path, sites: pd.DataFrame, qa: list[QAIssue],
                 average_splits: bool = False) -> pd.DataFrame:
    """One row per sample, cleaned. Values come from the workbook's last
    calculated values (Google Sheets includes these when you download .xlsx)."""
    d = pd.read_excel(xlsx, sheet_name=DATA_SHEET)
    d.columns = [str(c).strip() for c in d.columns]
    missing = [c for c in [COL_SITE, COL_DATE, COL_MPN] if c not in d.columns]
    if missing:
        sys.exit(f"Data tab is missing expected column(s): {missing}")
    d = d.dropna(subset=[COL_SITE, COL_DATE], how="any").copy()

    out = pd.DataFrame({
        "site_name_in_data": d[COL_SITE].astype(str).str.strip(),
        "date": pd.to_datetime(d[COL_DATE], errors="coerce"),
        "raw_mpn": d[COL_MPN],
        "notes": d.get(COL_NOTES, pd.Series(index=d.index, dtype=object)),
        "row": d.index + 2,  # spreadsheet row number, for QA messages
    })

    # Site ID: use the sheet's value; if blank/error, look the name up.
    lookup = _name_to_id_lookup(xlsx, sites)
    sid = pd.to_numeric(d.get(COL_SITE_ID), errors="coerce")
    by_name = out["site_name_in_data"].map(lambda n: lookup.get(_norm(n)))
    out["site_id"] = sid.fillna(by_name)
    unresolved = out["site_id"].isna()
    for n, g in out[unresolved].groupby("site_name_in_data"):
        qa.append(QAIssue("Site IDs", f"'{n}' ({len(g)} rows, e.g. row {g.row.iloc[0]}) "
                                      "has no Site ID and no matching name in Information."))
    out = out[~unresolved].copy()
    out["site_id"] = out["site_id"].astype(int)

    unknown = set(out.site_id) - set(sites.site_id)
    for s in sorted(unknown):
        qa.append(QAIssue("Site IDs", f"Site ID {s} appears in Data but not in Information."))

    # Name in Data vs. name in Information for the same ID
    info_name = sites.set_index("site_id")["site"]
    mism = out[out.site_id.map(info_name).map(_norm) != out.site_name_in_data.map(_norm)]
    for (sid_, n), g in mism.groupby(["site_id", "site_name_in_data"]):
        qa.append(QAIssue("Site names",
            f"Data row(s) {g.row.min()}–{g.row.max()} ({len(g)}): '{n}' vs Information "
            f"'{info_name.get(sid_, '?')}' (ID {sid_})."))

    # MPN
    parsed = out["raw_mpn"].map(parse_mpn)
    out["mpn"] = parsed.map(lambda t: t[0])
    out["censor"] = parsed.map(lambda t: t[1])
    for _, r in out[out.censor == "?"].iterrows():
        qa.append(QAIssue("MPN values", f"Row {r.row}: unreadable MPN '{r.raw_mpn}'."))
    blank = out[out.mpn.isna() & (out.censor != "?")]
    if len(blank):
        qa.append(QAIssue("MPN values",
            f"{len(blank)} row(s) have a date and site but no MPN result "
            f"(rows {', '.join(map(str, blank.row.head(10)))}"
            f"{'…' if len(blank) > 10 else ''})."))

    # Lab/field splits recorded in Notes, e.g. "<10 (lab split)"
    split_re = re.compile(r"^\s*([<>]?\s*\d+(?:\.\d+)?)\s*\((?:(?:lab|field)\s*split|duplicate)\)", re.I)
    split_val = out["notes"].map(lambda s: parse_mpn(split_re.match(str(s)).group(1))[0]
                                 if isinstance(s, str) and split_re.match(s) else np.nan)
    out["split_mpn"] = split_val
    if average_splits:  # FAQ: "Average the values"
        has = out.split_mpn.notna() & out.mpn.notna()
        out.loc[has, "mpn"] = (out.loc[has, "mpn"] + out.loc[has, "split_mpn"]) / 2

    # Rain
    rain_cols = [c for c in RAIN_COLS if c in d.columns]
    rain = pd.DataFrame({c: d[c].map(parse_rain) for c in rain_cols})
    use = rain_cols[:RAIN_DAYS]
    out["rain_in"] = rain[use].sum(axis=1, min_count=len(use))
    out["wet"] = np.where(out.rain_in.isna(), np.nan, out.rain_in >= WET_WEATHER_INCHES)
    # Days since the last day with >= WET_WEATHER_INCHES (0 = the sampling day itself; one more than
    # the number of rainfall columns = none in that week). Blank if that can't be told.
    daily = rain.to_numpy(dtype=float) if len(rain_cols) else np.full((len(out), 0), np.nan)
    hit = daily >= WET_WEATHER_INCHES
    first = np.where(hit.any(axis=1), hit.argmax(axis=1),
                     np.where(~np.isnan(daily).any(axis=1), daily.shape[1], np.nan))
    out["days_since_rain"] = np.where(np.isnan(daily).all(axis=1), np.nan, first) if daily.shape[1] else np.nan

    out["year"] = out.date.dt.year
    md = out.date.dt.month * 100 + out.date.dt.day
    out["in_season"] = md.between(SEASON_START[0] * 100 + SEASON_START[1],
                                  SEASON_END[0] * 100 + SEASON_END[1])
    out = out.merge(sites[["site_id", "site", "waterbody", "borough", "dec_class",
                           "lat", "lon", "currently_testing"]], on="site_id", how="left")
    out = out.dropna(subset=["date", "mpn"]).sort_values(["site_id", "date"]).reset_index(drop=True)

    # Other QA observations
    wd = out.date.dt.day_of_week
    nthu = int((wd != 3).sum())
    if nthu:
        qa.append(QAIssue("Rainfall",
            f"{nthu} samples were not collected on a Thursday, but rainfall columns are "
            "labeled by weekday (e.g. 'Previous Day Precipitation (Wednesday)')."))
    no_rain = out[out.rain_in.isna()].groupby("year").size()
    for y, n in no_rain.items():
        qa.append(QAIssue("Rainfall", f"{y}: {n} samples have no rainfall data."))
    dupe = out[out.duplicated(["site_id", "date"], keep=False)]
    if len(dupe):
        groups = dupe.groupby(["site_id", "date"]).row.apply(lambda r: "/".join(map(str, r)))
        more = "…" if len(groups) > 10 else ""
        qa.append(QAIssue("Duplicates",
            f"{len(groups)} site+date combinations have more than one row (Data rows "
            f"{', '.join(groups.head(10))}{more}). Geomeans count each row."))
    qc_re = re.compile(r"incubat|CFU|sterile|processed \d|hours", re.I)
    qc = out[out.notes.astype(str).str.contains(qc_re) & out.notes.notna()]
    for note, g in qc.groupby("notes"):
        qa.append(QAIssue("Lab QC notes (results kept; consider a QC-flag column)",
            f"'{note}': {len(g)} row(s), e.g. row {g.row.min()} ({g.date.min():%Y-%m-%d})."))
    return out


# =============================================================================
# SUMMARY STATISTICS
# =============================================================================

def geomean(x) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x) & (x > 0)]
    return float(np.exp(np.log(x).mean())) if len(x) else np.nan


def category(gm: float) -> str:
    if gm is None or not np.isfinite(gm):
        return "nodata"
    if gm <= GOOD_MAX:
        return "good"
    if gm <= ADVISORY_MAX:
        return "advisory"
    return "poor"


def summarize(g: pd.DataFrame) -> pd.Series:
    wet = g[g.wet == 1]
    dry = g[g.wet == 0]
    return pd.Series({
        "n": len(g),
        "geomean": geomean(g.mpn),
        "pct_over_limit": 100 * (g.mpn > SINGLE_SAMPLE_LIMIT).mean(),
        "geomean_dry": geomean(dry.mpn),
        "geomean_wet": geomean(wet.mpn),
        "n_dry": len(dry),
        "n_wet": len(wet),
        "first": g.date.min(),
        "last": g.date.max(),
        "last_mpn": g.sort_values("date").mpn.iloc[-1],
    })


def site_stats(samples: pd.DataFrame, by_year: bool = False) -> pd.DataFrame:
    keys = ["site_id", "year"] if by_year else ["site_id"]
    s = samples.groupby(keys).apply(summarize, include_groups=False).reset_index()
    s["n"] = s["n"].astype(int)
    s["category"] = s.geomean.map(category)
    return s


def rolling_window_stats(samples: pd.DataFrame, end: pd.Timestamp, days: int = 30) -> pd.DataFrame:
    """Stats for each site over the `days`-day window ending on `end` (inclusive),
    the same calculation as the GeoMean tabs."""
    start = end - pd.Timedelta(days=days - 1)
    w = samples[(samples.date >= start) & (samples.date <= end)]
    s = site_stats(w)
    s["window_start"], s["window_end"] = start, end
    return s


# =============================================================================
# VERIFY AGAINST THE WORKBOOK'S OWN GEOMEAN TABS
# =============================================================================

def verify_against_workbook(xlsx: Path, samples: pd.DataFrame) -> list[str]:
    """Recompute a few numbers the workbook already shows and report agreement.
    Uses openpyxl cached values; silently skipped if the tab layout changes."""
    msgs = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
        ws = wb["30 Day GeoMean Primary Contact "] if "30 Day GeoMean Primary Contact " in wb.sheetnames \
            else wb["30 Day GeoMean Primary Contact"]
        rows = list(ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=6, values_only=True))
        year = rows[0][0]
        start, end = rows[6][4], rows[6][5]  # E7, F7
        w = samples[(samples.date >= pd.Timestamp(start)) & (samples.date <= pd.Timestamp(end))
                    & (samples.year == year)]
        ytd = samples[samples.year == year]
        n_ok = n_tot = 0
        for r in rows[8:]:
            sid, cnt, ytd_gm, gm30 = r[1], r[2], r[3], r[4]
            if not isinstance(sid, (int, float)):
                continue
            ws_ = w[w.site_id == sid]
            mine = (len(ws_), geomean(ytd[ytd.site_id == sid].mpn), geomean(ws_.mpn))
            theirs = (cnt, ytd_gm, gm30)
            ok = True
            for a, b in zip(mine, theirs):
                if isinstance(b, (int, float)):
                    ok = ok and np.isfinite(a) and abs(a - b) < 0.01 * max(1, abs(b))
                else:  # the tab shows "No Data"
                    ok = ok and (a == 0 or not np.isfinite(a))
            n_ok += bool(ok)
            n_tot += 1
        msgs.append(f"30 Day GeoMean Primary Contact tab: {n_ok}/{n_tot} sites match "
                    f"(count, YTD geomean, 30-day geomean).")
    except Exception as e:  # pragma: no cover
        msgs.append(f"(Skipped workbook cross-check: {e})")
    return msgs


# =============================================================================
# CHARTS (matplotlib)
# =============================================================================


MIN_SAMPLES_PER_SEASON = 100   # seasons with fewer samples are left off the trend chart
MPN_FLOOR, MPN_CEIL = 7, 40000  # y-limits for MPN axes (log scale)
MPN_TICKS = [10, 100, 1000, 10000]


FONT_DIR = Path(__file__).resolve().parent / "templates" / "assets" / "fonts"
CHART_FOOTER = "SWIM Coalition · Community Water Quality Testing"   # left side of each chart's footer
_footer_note = ""   # set in main(): "Data through …"


def _style():
    """SWIM chart style: Poppins (from templates/assets/fonts), SWIM navy text."""
    import logging
    from matplotlib import font_manager
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)   # quiet font-fallback notes
    families = []
    for f in sorted(FONT_DIR.glob("*.ttf")) if FONT_DIR.is_dir() else []:
        font_manager.fontManager.addfont(str(f))
    if any(f.name == "Poppins" for f in font_manager.fontManager.ttflist):
        families.append("Poppins")
    plt.rcParams.update({
        # Characters Poppins lacks (e.g. ≥) fall back to DejaVu Sans automatically
        "font.family": families + ["DejaVu Sans"],
        "font.size": 9.5,
        "text.color": INK,
        "axes.labelcolor": INK_2,
        "axes.labelsize": 9.5,
        "xtick.color": MUTED, "ytick.color": MUTED,
        "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
        "axes.edgecolor": "#c3c2b7", "axes.linewidth": 0.8,
        "axes.facecolor": SURFACE, "figure.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.7, "grid.linestyle": "-",
        "axes.axisbelow": True,
        "axes.titlesize": 12.5, "axes.titleweight": "semibold", "axes.titlelocation": "left",
        "axes.titlecolor": INK,
        "axes.titlepad": 10,
        "legend.frameon": False, "legend.fontsize": 9,
        "savefig.dpi": 160, "savefig.bbox": "tight", "savefig.pad_inches": 0.25,
        "pdf.fonttype": 42,
    })


_LOGO_IMG = None


def _chart_logo():
    """The SWIM logo (templates/assets/swim-logo.png), trimmed of empty margins."""
    global _LOGO_IMG
    if _LOGO_IMG is None:
        path = TEMPLATE_DIR / "assets" / "swim-logo.png"
        _LOGO_IMG = False
        if path.exists():
            from PIL import Image
            im = Image.open(path).convert("RGBA")
            im = im.crop(im.getbbox())
            im.thumbnail((900, 150))
            _LOGO_IMG = np.asarray(im)
    return _LOGO_IMG


def _brand_footer(fig, logo_height_in=0.36, note=None):
    """Add a footer strip below everything on the figure: source text on the left and
    the SWIM logo on the right. Placed below the chart, so it never covers data."""
    renderer = fig.canvas.get_renderer()
    bb = fig.get_tightbbox(renderer)          # in inches
    fw, fh = fig.get_size_inches()
    top = bb.y0 - 0.08                         # just under the lowest element
    note = note if note is not None else _footer_note
    note = f"{CHART_FOOTER}  ·  {note}" if note else CHART_FOOTER
    fig.text(bb.x0 / fw, (top - logo_height_in / 2) / fh, note, ha="left", va="center",
             fontsize=8, color=INK_2)
    img = _chart_logo()
    if img is not False:
        w_in = logo_height_in * img.shape[1] / img.shape[0]
        ax = fig.add_axes([(bb.x1 - w_in) / fw, (top - logo_height_in) / fh, w_in / fw, logo_height_in / fh])
        ax.imshow(img, interpolation="lanczos")
        ax.set_axis_off()


def _fmt_int(v, _pos=None):
    return f"{int(round(v)):,}"


def _mpn_axis(ax, axis="y"):
    setter = ax.set_yscale if axis == "y" else ax.set_xscale
    setter("log")
    a = ax.yaxis if axis == "y" else ax.xaxis
    a.set_major_locator(FixedLocator(MPN_TICKS))
    a.set_minor_locator(NullLocator())
    a.set_major_formatter(FuncFormatter(_fmt_int))
    (ax.set_ylim if axis == "y" else ax.set_xlim)(MPN_FLOOR, MPN_CEIL)


def _quality_bands(ax, axis="y", tint=True, labels=True, label_pos=1.002):
    """Good / advisory / poor zones: light tints, dashed threshold lines, and
    zone labels placed outside the plot so they never collide with data labels.
    Call after the axis limits are set."""
    span = ax.axhspan if axis == "y" else ax.axvspan
    line = ax.axhline if axis == "y" else ax.axvline
    lo_lim, hi_lim = sorted(ax.get_ylim() if axis == "y" else ax.get_xlim())
    zones = [(lo_lim, GOOD_MAX, COLOR_GOOD, f"Good  ≤{GOOD_MAX}"),
             (GOOD_MAX, ADVISORY_MAX, COLOR_ADVISORY, f"Advisory  {GOOD_MAX}–{ADVISORY_MAX}"),
             (ADVISORY_MAX, hi_lim, COLOR_POOR, f"Poor  >{ADVISORY_MAX}")]
    for lo, hi, c, _ in zones:
        if tint:
            span(lo, hi, color=c, alpha=0.08, lw=0, zorder=0)
    for v in (GOOD_MAX, ADVISORY_MAX):
        line(v, color=INK_2, lw=0.9, ls=(0, (4, 3)), alpha=0.7, zorder=1)
    if not labels:
        return
    for lo, hi, c, txt in zones:
        mid = math.sqrt(lo * hi)
        if axis == "y":
            tr = ax.get_yaxis_transform()
            # a colored rail just outside the right edge, with the label beside it
            ax.plot([1.012, 1.012], [lo * 1.04, hi / 1.04], transform=tr, color=c, lw=4,
                    solid_capstyle="butt", clip_on=False, zorder=5)
            ax.text(1.026, mid, txt.replace("  ", "\n", 1), transform=tr, ha="left",
                    va="center", fontsize=8, color=INK_2, linespacing=1.1, clip_on=False)
        else:
            tr = ax.get_xaxis_transform()
            ax.text(mid, label_pos, txt, transform=tr, ha="center", va="bottom", fontsize=8,
                    color=INK_2)


def _subtitle(ax, text, y=1.0):
    ax.annotate(text, xy=(0, y), xycoords="axes fraction", xytext=(0, 6),
                textcoords="offset points", fontsize=9, color=INK_2, va="bottom", ha="left")


def short_name(site: str, waterbody: str) -> str:
    pre = f"{waterbody}, "
    return site[len(pre):] if site.startswith(pre) else site


# ---- 1. Program trend -------------------------------------------------------

def chart_program_trend(season, site_year, out: Path, tables: Path):
    per_season = season.groupby("year").size()
    years = [int(y) for y in per_season.index if per_season[y] >= MIN_SAMPLES_PER_SEASON]
    sy = site_year[(site_year.n >= MIN_SAMPLES_FOR_GEOMEAN) & site_year.year.isin(years)]

    agg = sy.groupby("year").geomean.agg(
        median_site_geomean="median",
        q25=lambda x: x.quantile(0.25), q75=lambda x: x.quantile(0.75), n_sites="size")
    s = season[season.year.isin(years)].assign(over=lambda x: x.mpn > SINGLE_SAMPLE_LIMIT)
    agg["pct_over_all"] = s.groupby("year").over.mean() * 100
    agg["pct_over_dry"] = s[s.wet == 0].groupby("year").over.mean() * 100
    agg["pct_over_wet"] = s[s.wet == 1].groupby("year").over.mean() * 100
    agg["n_samples"] = s.groupby("year").size()
    _r(agg).to_csv(tables / "program_trend_by_season.csv")

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 8.6), sharex=True,
                                 gridspec_kw={"height_ratios": [1.15, 1], "hspace": 0.36})
    x = agg.index.values
    _mpn_axis(a1)
    a1.set_ylim(MPN_FLOOR, 2000)
    _quality_bands(a1, tint=False)
    a1.fill_between(x, agg.q25, agg.q75, color=SERIES, alpha=0.10, lw=0, zorder=2)
    a1.plot(x, agg.median_site_geomean, color=SERIES, lw=2, marker="o", ms=5, mec=SURFACE,
            mew=1.5, zorder=4)
    a1.annotate(f"{agg.median_site_geomean.iloc[-1]:,.0f}",
                (x[-1], agg.median_site_geomean.iloc[-1]), xytext=(8, 0),
                textcoords="offset points", va="center", fontsize=9, color=INK, fontweight="bold")
    a1.set_ylabel("Seasonal geometric mean\n(MPN / 100 ml, log scale)")
    a1.set_title("Typical site water quality, by season", pad=28)
    _subtitle(a1, f"Line = the median site; shaded band = the middle 50% of sites. Each site's "
                  f"seasonal geomean needs ≥{MIN_SAMPLES_FOR_GEOMEAN} samples.")
    a1.set_xlim(x.min() - 0.5, x.max() + 0.7)
    a1.grid(axis="x", visible=False)

    series = [("pct_over_all", "All samples", MUTED, 1.8),
              ("pct_over_dry", f"Dry weather (<{WET_WEATHER_INCHES}\" rain)", SERIES, 2),
              ("pct_over_wet", f"Wet weather (≥{WET_WEATHER_INCHES}\" rain)", SERIES_2, 2)]
    for col, lab, c, lw in series:
        v = agg[col].dropna()
        a2.plot(v.index, v.values, color=c, lw=lw, marker="o", ms=4.5, mec=SURFACE, mew=1.2,
                label=lab, zorder=3)
        if len(v):
            a2.annotate(f"{v.iloc[-1]:.0f}%", (v.index[-1], v.iloc[-1]), xytext=(7, 0),
                        textcoords="offset points", va="center", fontsize=8.5, color=INK_2)
    a2.set_ylim(0, 100)
    a2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    a2.set_ylabel(f"Samples over {SINGLE_SAMPLE_LIMIT} MPN")
    a2.set_title(f"Share of samples over the single-sample limit ({SINGLE_SAMPLE_LIMIT})", pad=28)
    rain_txt = (f"Wet weather = ≥{WET_WEATHER_INCHES}\" of rain at Central Park on the sampling "
                f"day and the {RAIN_DAYS - 1} days before.")
    no_rain = [y for y in years if pd.isna(agg.loc[y, "pct_over_dry"]) and pd.isna(agg.loc[y, "pct_over_wet"])]
    if no_rain:
        rain_txt += f" No rainfall entered yet for {', '.join(map(str, no_rain))}."
    _subtitle(a2, rain_txt)
    a2.legend(loc="upper left", ncols=3, fontsize=8.5, handlelength=1.6)
    a2.set_xticks(x)
    a2.set_xticklabels([f"{y}\n{int(agg.loc[y, 'n_sites'])}" for y in x], fontsize=8.5)
    a2.set_xlabel("Season  (number underneath = sites with a seasonal geomean)", fontsize=8.5)
    a2.grid(axis="x", visible=False)
    _brand_footer(fig)
    fig.savefig(out)
    plt.close(fig)


# ---- 7. Site x season heatmap ----------------------------------------------

def chart_site_heatmap(site_year, sites, out: Path, tables: Path):
    sy = site_year.merge(sites[["site_id", "site", "waterbody"]], on="site_id")
    years = list(range(int(sy.year.min()), int(sy.year.max()) + 1))
    wide = sy.pivot(index="site_id", columns="year", values="geomean").reindex(columns=years)
    nwide = sy.pivot(index="site_id", columns="year", values="n").reindex(columns=years)
    meta = sites.set_index("site_id").loc[wide.index, ["site", "waterbody"]]
    _r(meta.join(wide).sort_values(["waterbody", "site"])).to_csv(tables / "site_by_season_geomean.csv")

    rows, labels, is_header = [], [], []
    for wb_name, g in meta.sort_values(["waterbody", "site"]).groupby("waterbody", sort=True):
        rows.append(None)
        labels.append(wb_name)
        is_header.append(True)
        for sid, r in g.iterrows():
            rows.append(sid)
            labels.append(short_name(r.site, wb_name))
            is_header.append(False)

    code = {"good": 1, "advisory": 2, "poor": 3}
    M = np.full((len(rows), len(years)), np.nan)
    small = []
    for i, sid in enumerate(rows):
        if sid is None:
            continue
        for j, y in enumerate(years):
            gm = wide.loc[sid, y]
            if np.isfinite(gm):
                M[i, j] = code[category(gm)]
                if nwide.loc[sid, y] < MIN_SAMPLES_FOR_GEOMEAN:
                    small.append((j, i))
            else:
                M[i, j] = 0  # not sampled that season

    cmap = ListedColormap(["#f0efec", COLOR_GOOD, COLOR_ADVISORY, COLOR_POOR])
    fig_h = len(rows) * 0.17 + 1.6
    fig, ax = plt.subplots(figsize=(10.5, fig_h))
    ax.pcolormesh(np.arange(len(years) + 1), np.arange(len(rows) + 1), np.ma.masked_invalid(M),
                  cmap=cmap, vmin=-0.5, vmax=3.5, edgecolors=SURFACE, linewidth=1.6)
    if small:
        sx, sy_ = zip(*small)
        ax.scatter(np.array(sx) + 0.5, np.array(sy_) + 0.5, s=6, color=INK, alpha=0.6, lw=0, zorder=3)
    ax.set_ylim(len(rows), 0)
    ax.set_xlim(0, len(years))
    ax.set_yticks(np.arange(len(rows)) + 0.5)
    ax.set_yticklabels(labels, fontsize=7.2)
    for t, h in zip(ax.get_yticklabels(), is_header):
        if h:
            t.set_fontweight("bold")
            t.set_color(INK)
            t.set_fontsize(8)
    ax.tick_params(axis="y", length=0, pad=4)
    ax.set_xticks(np.arange(len(years)) + 0.5)
    ax.set_xticklabels(years, fontsize=8)
    ax.xaxis.tick_top()
    ax.tick_params(axis="x", length=0, pad=3)
    ax.grid(False)
    for sp in ax.spines.values():
        sp.set_visible(False)

    handles = [Patch(color=COLOR_GOOD, label=CATEGORY_LABELS["good"]),
               Patch(color=COLOR_ADVISORY, label=CATEGORY_LABELS["advisory"]),
               Patch(color=COLOR_POOR, label=CATEGORY_LABELS["poor"]),
               Patch(color="#f0efec", label="Not sampled"),
               Line2D([], [], marker="o", ls="", color=INK, alpha=0.6, ms=3,
                      label=f"Fewer than {MIN_SAMPLES_FOR_GEOMEAN} samples")]
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1),
              bbox_transform=offset_copy(ax.transAxes, fig=fig, y=20, units="points"),
              ncols=5, fontsize=8, handlelength=1.2, columnspacing=1.2, borderaxespad=0)
    ax.annotate("Sites grouped by waterbody. Color = that season's geomean (MPN / 100 ml), "
                "regular-season samples only.", xy=(0, 1), xycoords="axes fraction",
                xytext=(0, 44), textcoords="offset points", fontsize=9, color=INK_2)
    ax.set_title("Seasonal geometric mean at every site", pad=62)
    _brand_footer(fig)
    fig.savefig(out)
    plt.close(fig)


# ---- 3. Rainfall effect -----------------------------------------------------

RAIN_EDGES = [0, 0.005, 0.25, 0.5, 1.0, np.inf]   # inches; bins include the left edge
RAIN_LABELS = ["None\n(0 or trace)", "0.01–0.24\"", "0.25–0.49\"", "0.50–0.99\"", "1\" or more"]


def chart_rainfall(season, out: Path, tables: Path):
    s = season.dropna(subset=["rain_in"]).copy()
    s["bin"] = pd.cut(s.rain_in, RAIN_EDGES, right=False, labels=RAIN_LABELS)
    rows = []
    for lab in RAIN_LABELS:
        g = s[s.bin == lab].mpn
        rows.append({"rain_bin": lab.replace("\n", " "), "n_samples": len(g), "geomean": geomean(g),
                     "q25": g.quantile(0.25), "median": g.median(), "q75": g.quantile(0.75),
                     "pct_over_limit": 100 * (g > SINGLE_SAMPLE_LIMIT).mean()})
    t = pd.DataFrame(rows)
    _r(t).to_csv(tables / "rainfall_effect.csv", index=False)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.5, 4.9), gridspec_kw={"wspace": 0.42})
    x = np.arange(len(RAIN_LABELS))
    _mpn_axis(a1)
    a1.set_ylim(MPN_FLOOR, 3000)
    a1.set_xlim(-0.5, len(RAIN_LABELS) - 0.5)
    _quality_bands(a1)
    a1.vlines(x, t.q25, t.q75, color=SERIES, lw=2.5, alpha=0.35, zorder=2, capstyle="round")
    a1.plot(x, t.geomean, ls="", marker="o", ms=8, color=SERIES, mec=SURFACE, mew=2, zorder=3)
    for xi, gm in zip(x, t.geomean):
        a1.annotate(f"{gm:,.0f}", (xi, gm), xytext=(9, 0), textcoords="offset points",
                    va="center", fontsize=8.5, color=INK,
                    bbox=dict(boxstyle="square,pad=0.15", fc=SURFACE, ec="none", alpha=0.8))
    a1.set_xticks(x)
    a1.set_xticklabels(RAIN_LABELS, fontsize=8.5)
    a1.grid(axis="x", visible=False)
    a1.set_ylabel("MPN / 100 ml (log scale)")
    a1.set_xlabel(f"Rain at Central Park, sampling day + {RAIN_DAYS - 1} days before")
    a1.set_title("Bacteria levels rise with rainfall", pad=28)
    _subtitle(a1, "Dot = geometric mean; line = middle 50% of samples")

    bars = a2.bar(x, t.pct_over_limit, width=0.55, color=SERIES, zorder=2)
    for b, v in zip(bars, t.pct_over_limit):
        a2.annotate(f"{v:.0f}%", (b.get_x() + b.get_width() / 2, v), xytext=(0, 3),
                    textcoords="offset points", ha="center", va="bottom", fontsize=9, color=INK)
    a2.set_xticks(x)
    a2.set_xticklabels([f"{l}\nn={n:,}" for l, n in zip(RAIN_LABELS, t.n_samples)], fontsize=8.5)
    a2.set_ylim(0, 100)
    a2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    a2.grid(axis="x", visible=False)
    a2.set_xlabel(f"Rain at Central Park, sampling day + {RAIN_DAYS - 1} days before")
    a2.set_title(f"Share of samples over {SINGLE_SAMPLE_LIMIT} MPN", pad=28)
    _subtitle(a2, f"All sites, {int(s.year.min())}–{int(s.year.max())} seasons with rainfall data "
                  f"({len(s):,} samples)")
    _brand_footer(fig)
    fig.savefig(out)
    plt.close(fig)


# ---- 2. Waterbody comparison ------------------------------------------------

def _right_col(ax, y, text, x=1.01, **kw):
    kw.setdefault("fontsize", 8)
    kw.setdefault("color", INK_2)
    ax.annotate(text, (x, y), xycoords=("axes fraction", "data"), va="center", ha="left", **kw)


def chart_waterbodies(season, out: Path, tables: Path):
    last = int(season.year.max())
    first = last - RECENT_SEASONS + 1
    s = season[season.year >= first]
    site_gm = s.groupby(["waterbody", "site_id"]).mpn.agg(gm=geomean, n="size").reset_index()
    site_gm = site_gm[site_gm.n >= MIN_SAMPLES_FOR_GEOMEAN]
    wb = s.groupby("waterbody").agg(geomean=("mpn", geomean), n_samples=("mpn", "size"),
                                    n_sites=("site_id", "nunique"),
                                    pct_over=("mpn", lambda v: 100 * (v > SINGLE_SAMPLE_LIMIT).mean()))
    wb = wb[wb.n_samples >= 10].sort_values("geomean")
    _r(wb).to_csv(tables / f"waterbody_geomean_{first}_{last}.csv")

    fig_h = 0.3 * len(wb) + 1.6
    fig, ax = plt.subplots(figsize=(10, fig_h))
    _mpn_axis(ax, "x")
    ax.set_xlim(MPN_FLOOR, 5000)
    ax.set_ylim(len(wb) - 0.4, -0.6)
    _quality_bands(ax, axis="x")
    ypos = {w: i for i, w in enumerate(wb.index)}
    sg = site_gm[site_gm.waterbody.isin(wb.index)]
    ax.scatter(sg.gm, sg.waterbody.map(ypos), s=22, color=SERIES, alpha=0.35, lw=0, zorder=2)
    ax.scatter(wb.geomean, np.arange(len(wb)), s=70, color=SERIES, edgecolor=SURFACE, lw=2, zorder=3)
    _right_col(ax, -1.0, "Geomean", fontsize=8, color=INK, fontweight="bold")
    _right_col(ax, -1.0, "Sites · samples", x=1.105, fontsize=8, color=INK, fontweight="bold")
    for i, (w, r) in enumerate(wb.iterrows()):
        _right_col(ax, i, f"{r.geomean:,.0f}", color=INK, fontsize=8.5)
        _right_col(ax, i, f"{int(r.n_sites)} · {int(r.n_samples):,}", x=1.105)
    ax.set_yticks(np.arange(len(wb)))
    ax.set_yticklabels(wb.index, fontsize=8.5)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Geometric mean, MPN / 100 ml (log scale)")
    ax.set_title(f"Recent water quality by waterbody, {first}–{last} seasons", pad=44)
    ax.annotate(f"Large dot = all samples in the waterbody; small dots = individual sites "
                f"(≥{MIN_SAMPLES_FOR_GEOMEAN} samples). Cleanest at top; waterbodies with "
                f"<10 samples left out.", xy=(0, 1), xycoords="axes fraction", xytext=(0, 24),
                textcoords="offset points", fontsize=8.8, color=INK_2)
    _brand_footer(fig)
    fig.savefig(out)
    plt.close(fig)


# ---- 4. Recovery after rain ------------------------------------------------

def waterbody_group(site_id, waterbody) -> str:
    if int(site_id) in SITE_GROUP_OVERRIDES:
        return SITE_GROUP_OVERRIDES[int(site_id)]
    return next((g for g, wbs in WATERBODY_GROUPS.items() if waterbody in wbs), "Major rivers & harbor")


def chart_rain_recovery(season, out: Path, tables: Path):
    d = season.dropna(subset=["days_since_rain"]).copy()
    d["k"] = d.days_since_rain.astype(int)
    d["group"] = [waterbody_group(i, w) for i, w in zip(d.site_id, d.waterbody)]
    kmax = len(RAIN_COLS)
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    _mpn_axis(ax)
    ax.set_ylim(MPN_FLOOR, 3000)
    ax.set_xlim(-0.5, kmax + 0.5)
    _quality_bands(ax)
    rows = []
    for gname, color in GROUP_COLORS.items():
        g = d[d.group == gname].groupby("k").mpn.agg(["size", geomean])
        g = g[g["size"] >= 25]
        ax.plot(g.index, g["geomean"], color=color, lw=2, marker="o", ms=5, mec=SURFACE, mew=1.2,
                label=gname, zorder=3)
        rows += [{"group": gname, "days_since_rain": k, "n_samples": int(r["size"]), "geomean": r["geomean"]}
                 for k, r in g.iterrows()]
    allg = d.groupby("k").mpn.agg(n="size", geomean=geomean,
                                  pct_over=lambda v: 100 * (v > SINGLE_SAMPLE_LIMIT).mean())
    rows += [{"group": "All sites", "days_since_rain": k, "n_samples": int(r.n), "geomean": r.geomean,
              "pct_over_limit": r.pct_over} for k, r in allg.iterrows()]
    _r(pd.DataFrame(rows)).to_csv(tables / "rain_recovery.csv", index=False)
    labels = ["Same\nday"] + [f"{k} day{'s' if k > 1 else ''}" for k in range(1, kmax)] + \
             [f"No rain\nin {kmax} days"]
    ax.set_xticks(range(kmax + 1))
    ax.set_xticklabels(labels)
    for k, r in allg.iterrows():
        ax.text(k, 8.3, f"{r.pct_over:.0f}%", ha="center", fontsize=8, color=INK_2)
    ax.text(-0.45, 11, f"% of all samples over {SINGLE_SAMPLE_LIMIT}:", fontsize=7.6, color=INK_2, ha="left")
    ax.grid(axis="x", visible=False)
    ax.set_xlabel(f"Days since the last day with ≥{WET_WEATHER_INCHES}\" of rain (Central Park)")
    ax.set_ylabel("Geometric mean\n(MPN / 100 ml, log scale)")
    ax.set_title("How long after rain does water quality recover?", pad=28)
    _subtitle(ax, "Regular-season samples. “Same day” rain may have fallen after the sample was taken.")
    ax.legend(loc="upper right", fontsize=8)
    _brand_footer(fig)
    fig.savefig(out)
    plt.close(fig)


# ---- 5. Green infrastructure installations ----------------------------------

def load_gi() -> tuple[pd.DataFrame, pd.Timestamp | None]:
    """Every green infrastructure asset from the GI_LAYER files in data/: its asset type,
    category (vegetated / green-grey), whether it's finished (same rule as the map) and, for
    finished ones, the construction date. Also returns the download date from the file names."""
    cfg_path = DATA_DIR / "layers.json"
    if not cfg_path.exists():
        return pd.DataFrame(), None
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    L = next((l for l in cfg.get("layers", []) if l.get("id") == GI_LAYER), None)
    if not L or not L.get("complete"):
        return pd.DataFrame(), None
    green_grey = set(cfg.get("lists", {}).get(GI_GREEN_GREY_LIST, []))
    names = L.get("values", {}).get(L.get("title"), {})
    today = pd.Timestamp.now().normalize()
    rows, downloaded = [], None
    for pattern in L["files"]:
        matches = sorted(DATA_DIR.glob(pattern))
        if not matches:
            continue
        f = matches[-1]
        m = re.search(r"(20\d\d)(\d\d)(\d\d)", f.name)
        if m:
            d = pd.Timestamp(int(m[1]), int(m[2]), int(m[3]))
            downloaded = d if downloaded is None else max(downloaded, d)
        for feat in json.loads(f.read_text(encoding="utf-8")).get("features", []):
            p = feat.get("properties") or {}
            t = str(_clean_value(p.get("asset_type")) or "")
            done, _ = _gi_done(p, L["complete"], L.get("built_date"), today)
            built = _gi_built_date(p, L["built_date"], today) if done and L.get("built_date") else None
            rows.append({"asset_type": t, "name": names.get(t, t),
                         "category": "Green-grey" if t in green_grey else "Vegetated",
                         "finished": done, "built": built})
    return pd.DataFrame(rows), downloaded


def _gi_type_label(name: str) -> str:
    """'ROW Infiltration Basin with Concrete Top' -> 'Right-of-way infiltration basin, concrete top'"""
    if name.startswith("ROW "):
        name = "Right-of-way " + name[4:]
    name = name.replace(" with ", ", ")
    return name[:1] + name[1:].lower()


def chart_green_infrastructure(gi: pd.DataFrame, downloaded, out: Path, tables: Path):
    from matplotlib.gridspec import GridSpec
    import textwrap
    cats = list(GI_COLORS)
    done = gi[gi.finished]
    dated = done.dropna(subset=["built"])
    years = dated.built.dt.year.astype(int)
    last = int(downloaded.year if downloaded is not None else years.max())
    span = list(range(GI_FIRST_YEAR, last + 1))
    per_year = dated.groupby([years.clip(lower=GI_FIRST_YEAR), "category"]).size().unstack(fill_value=0)
    cum = per_year.reindex(span, fill_value=0).reindex(columns=cats, fill_value=0).cumsum()
    pipe = gi[~gi.finished].category.value_counts().reindex(cats, fill_value=0)
    t = cum.copy()
    t.columns = [f"{c.lower()}_cumulative" for c in cats]
    t["new_this_year"] = per_year.reindex(span, fill_value=0).sum(axis=1)
    _r(t.rename_axis("year").reset_index()).to_csv(tables / "green_infrastructure_by_year.csv", index=False)

    fig = plt.figure(figsize=(12.6, 5.6))
    gs = GridSpec(1, 2, width_ratios=[1.75, 1], wspace=0.12, figure=fig)
    ax = fig.add_subplot(gs[0])
    x = np.arange(len(span))
    xp = len(span) + 0.9                       # the pipeline bar, set apart from the years
    bottom = np.zeros(len(span))
    for c in cats:
        ax.bar(x, cum[c].values, 0.78, bottom=bottom, color=GI_COLORS[c], edgecolor=SURFACE, lw=0.8,
               label=f"{c} (built)", zorder=3)
        bottom += cum[c].values
    pb = 0
    plt.rcParams["hatch.linewidth"] = 0.8
    for c in cats:
        ax.bar(xp, pipe[c], 0.78, bottom=pb, facecolor=GI_COLORS[c] + "4d", edgecolor=GI_BORDERS[c],
               hatch="////", lw=1.1, ls=(0, (3, 2)), zorder=3)
        pb += pipe[c]
    top = max(bottom.max(), pb)
    for i in [i for i, y in enumerate(span) if y % 5 == 0 and y < last - 1] + [len(span) - 1]:
        ax.text(x[i], bottom[i] + top * 0.012, f"{bottom[i]:,.0f}", ha="center", va="bottom",
                fontsize=8, color=INK_2)
    ax.text(xp, pb + top * 0.012, f"{pb:,.0f}", ha="center", va="bottom", fontsize=8, color=INK_2)
    ax.set_xticks(list(x) + [xp])
    ax.set_xticklabels([f"’{y % 100:02d}" if y != last else f"{y}*" for y in span] + ["Pipeline"],
                       fontsize=8)
    for lab in ax.get_xticklabels()[-2:]:
        lab.set_fontweight("semibold")
    ax.set_xlim(-0.7, xp + 0.7)
    ax.set_ylim(0, top * 1.1)
    ax.yaxis.set_major_formatter(FuncFormatter(_fmt_int))
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Installations built (running total)")
    ax.set_title("Green infrastructure installations in NYC", pad=28)
    _subtitle(ax, f"Running total of finished installations by the year built, and projects still in design or construction.")
    handles = [Patch(facecolor=GI_COLORS[c], edgecolor=SURFACE, label=c) for c in cats] + \
              [Patch(facecolor="#ffffff", edgecolor=GI_BORDERS["Green-grey"], hatch="////", ls=(0, (3, 2)),
                     label="Pipeline (in design or construction)")]
    ax.legend(handles=handles, loc="upper left", fontsize=8.5)
    undated = len(done) - len(dated)
    early = int((years < GI_FIRST_YEAR).sum())
    notes = [f"* {last} as of the data download"
             + (f" ({downloaded:%b %-d, %Y})" if downloaded is not None else "") + "."]
    if early:
        notes.append(f"’{GI_FIRST_YEAR % 100:02d} includes {early} built earlier.")
    if undated:
        notes.append(f"{undated} finished installations without a valid construction date aren't counted.")
    ax.annotate("\n".join(textwrap.wrap("  ".join(notes), 110)), xy=(0, 0), xycoords="axes fraction", xytext=(0, -30),
                textcoords="offset points", fontsize=7.6, color=INK_2, va="top", ha="left")

    # Right: most common types among finished installations
    axp = fig.add_subplot(gs[1])
    counts = done.groupby(["name", "category"]).size().sort_values(ascending=False)
    top_t = counts.iloc[:GI_TOP_TYPES]
    other = int(counts.iloc[GI_TOP_TYPES:].sum())
    labels = [_gi_type_label(n) for n, _ in top_t.index] + (["Other types"] if other else [])
    vals = list(top_t.values) + ([other] if other else [])
    shades = {"Vegetated": ["#2b8a3e", "#4fa85d", "#78c083", "#a3d6a9", "#c6e6c9"],
              "Green-grey": ["#9aa892", "#b5c0ae", "#cdd5c8", "#e0e5dc"]}
    used = {c: 0 for c in cats}
    colors = []
    for _, c in top_t.index:
        colors.append(shades[c][min(used[c], len(shades[c]) - 1)])
        used[c] += 1
    if other:
        colors.append("#e7e6e0")
    tot = sum(vals)
    wedges, _ = axp.pie(vals, colors=colors, startangle=90, counterclock=False,
                        wedgeprops={"edgecolor": SURFACE, "linewidth": 1.5}, radius=1.0)
    for w, v in zip(wedges, vals):          # percentages inside the larger slices
        if v / tot >= 0.06:
            ang = np.deg2rad((w.theta1 + w.theta2) / 2)
            dark = w.get_facecolor()[:3] in [matplotlib.colors.to_rgb(c) for c in ("#2b8a3e", "#4fa85d")]
            axp.text(0.66 * np.cos(ang), 0.66 * np.sin(ang), f"{100 * v / tot:.0f}%", ha="center",
                     va="center", fontsize=9, fontweight="semibold", color=SURFACE if dark else INK)
    key = [Patch(facecolor=c, edgecolor="#c3c2b7" if c == "#e7e6e0" else c,
                 label=f"{lab}  ·  {v:,} ({100 * v / tot:.0f}%)") for c, lab, v in zip(colors, labels, vals)]
    axp.legend(handles=key, loc="upper center", bbox_to_anchor=(0.5, -0.03), ncol=1, fontsize=8,
               handlelength=1.1, handleheight=1.1, labelspacing=0.45, borderaxespad=0)
    axp.set_aspect("equal")
    axp.set_title("Most common types of finished installations", fontsize=10.5, pad=24, loc="center")
    axp.annotate("Green shades are vegetated types, gray-green shades green-grey", xy=(0.5, 1.0),
                 xycoords="axes fraction", xytext=(0, 6), textcoords="offset points", ha="center",
                 va="bottom", fontsize=8, color=INK_2)
    _r(counts.rename("installations").reset_index()).to_csv(tables / "green_infrastructure_types.csv",
                                                             index=False)
    src = "Green infrastructure: NYC DEP via NYC Open Data"
    if downloaded is not None:
        src += f", downloaded {downloaded:%b %-d, %Y}"
    _brand_footer(fig, note=src)
    fig.savefig(out)
    plt.close(fig)


# ---- 6. Latest 30 days, by waterbody ----------------------------------------

def chart_latest_window(samples, win, out: Path, tables: Path):
    start, end = win.window_start.iloc[0], win.window_end.iloc[0]
    s = samples[(samples.date >= start) & (samples.date <= end)]
    site_gm = s.groupby(["waterbody", "site_id"]).mpn.agg(gm=geomean, n="size").reset_index()
    wb = s.groupby("waterbody").agg(geomean=("mpn", geomean), n_samples=("mpn", "size"),
                                    n_sites=("site_id", "nunique"),
                                    pct_over=("mpn", lambda v: 100 * (v > SINGLE_SAMPLE_LIMIT).mean()))
    wb = wb.sort_values("geomean")
    wb.assign(window_start=start, window_end=end).pipe(_r).to_csv(tables / "latest_30_days_by_waterbody.csv")

    fig, ax = plt.subplots(figsize=(10, 0.3 * len(wb) + 1.6))
    _mpn_axis(ax, "x")
    ax.set_ylim(len(wb) - 0.4, -0.6)
    _quality_bands(ax, axis="x")
    ypos = {w: i for i, w in enumerate(wb.index)}
    ax.scatter(site_gm.gm, site_gm.waterbody.map(ypos), s=22, color=SERIES, alpha=0.35, lw=0, zorder=2)
    full = (wb.n_samples >= MIN_SAMPLES_FOR_GEOMEAN).values
    y = np.arange(len(wb))
    ax.scatter(wb.geomean[full], y[full], s=70, color=SERIES, edgecolor=SURFACE, lw=2, zorder=3)
    ax.scatter(wb.geomean[~full], y[~full], s=70, facecolor=SURFACE, edgecolor=SERIES, lw=2, zorder=3)
    _right_col(ax, -1.0, "Geomean", color=INK, fontweight="bold")
    _right_col(ax, -1.0, f"% over {SINGLE_SAMPLE_LIMIT}", x=1.095, color=INK, fontweight="bold")
    _right_col(ax, -1.0, "Sites · samples", x=1.2, color=INK, fontweight="bold")
    for i, (w, r) in enumerate(wb.iterrows()):
        _right_col(ax, i, f"{r.geomean:,.0f}", color=INK, fontsize=8.5)
        _right_col(ax, i, f"{r.pct_over:.0f}%", x=1.095)
        _right_col(ax, i, f"{int(r.n_sites)} · {int(r.n_samples)}", x=1.2)
    ax.set_yticks(y)
    ax.set_yticklabels(wb.index, fontsize=8.5)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("30-day geometric mean, MPN / 100 ml (log scale)")
    ax.set_title(f"Water quality over the latest 30 days, {start:%b %-d} – {end:%b %-d, %Y}", pad=44)
    ax.annotate(f"Large dot = all samples in the waterbody; small dots = individual sites. Cleanest at top. "
                f"Hollow dot = fewer than {MIN_SAMPLES_FOR_GEOMEAN} samples.", xy=(0, 1),
                xycoords="axes fraction", xytext=(0, 24), textcoords="offset points",
                fontsize=8.8, color=INK_2)
    _brand_footer(fig)
    fig.savefig(out)
    plt.close(fig)


# ---- 8. Per-site time series PDF -------------------------------------------

def _season_x(dates: pd.Series, seasons: list[int]) -> pd.Series:
    """Place each date inside its season's slot: season i spans x = i .. i+1."""
    idx = {y: i for i, y in enumerate(seasons)}
    yr = dates.dt.year
    st = pd.to_datetime(dict(year=yr, month=SEASON_START[0], day=SEASON_START[1]))
    en = pd.to_datetime(dict(year=yr, month=SEASON_END[0], day=SEASON_END[1]))
    frac = ((dates - st) / (en - st)).clip(0, 1)
    return yr.map(idx) + 0.07 + 0.86 * frac


def site_pdf_name(site_id: int) -> str:
    """Path (relative to the output folder) of a site's own time-series PDF."""
    return f"sites/site-{int(site_id):03d}.pdf"


def site_geomean_chart(g, srow, heading: str, place: str):
    """Page 1 of each site's PDF: the seasonal geometric mean as a simple line over the years,
    each point colored good / advisory / poor and labeled with its value."""
    by = g.groupby("year").mpn.agg(gm=geomean, n="size").reset_index()
    by["year"] = by.year.astype(int)
    years = list(range(int(by.year.min()), int(by.year.max()) + 1))
    fig, ax = plt.subplots(figsize=(11, 4.8))
    _mpn_axis(ax)
    top = 5000 if by.gm.max() < 2500 else MPN_CEIL
    ax.set_ylim(MPN_FLOOR, top)
    ax.set_xlim(years[0] - 0.6, years[-1] + 0.6)
    _quality_bands(ax)
    # line, broken where a season wasn't sampled
    run = [by.iloc[0]]
    for (_, a), (_, b) in zip(by.iterrows(), by.iloc[1:].iterrows()):
        if b.year - a.year == 1:
            run.append(b)
        else:
            ax.plot([r.year for r in run], [r.gm for r in run], color=SERIES, lw=2, zorder=3,
                    solid_capstyle="round")
            run = [b]
    ax.plot([r.year for r in run], [r.gm for r in run], color=SERIES, lw=2, zorder=3,
            solid_capstyle="round")
    for _, r in by.iterrows():
        c = CATEGORY_COLORS[category(r.gm)]
        few = r.n < MIN_SAMPLES_FOR_GEOMEAN
        ax.scatter([r.year], [r.gm], s=70, zorder=4, linewidths=2 if few else 1.5,
                   facecolor=SURFACE if few else c, edgecolor=c if few else SURFACE)
        ax.annotate(f"{r.gm:,.0f}", (r.year, r.gm), xytext=(0, 9), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8.5, color=INK, zorder=5,
                    bbox=dict(boxstyle="square,pad=0.1", fc=SURFACE, ec="none", alpha=0.7))
    sampled = set(by.year)
    ax.set_xticks(years)
    ax.set_xticklabels([str(y) for y in years], fontsize=8.5 if len(years) < 14 else 7.8)
    for lab, y in zip(ax.get_xticklabels(), years):
        if y not in sampled:
            lab.set_color("#b3b9c6")
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Seasonal geometric mean\n(MPN / 100 ml, log scale)")
    ax.set_xlabel("Season  (gray year = not sampled)" if len(sampled) < len(years) else "Season", fontsize=8.5)
    ax.set_title(heading, pad=28)
    _subtitle(ax, f"{place} · Site ID {srow.site_id} · Seasonal geometric mean of each season's "
                  f"regular samples (May–early October)")
    handles = [Line2D([], [], marker="o", ls="", ms=8, mfc=CATEGORY_COLORS[k], mec=SURFACE,
                      label=CATEGORY_LABELS[k]) for k in ("good", "advisory", "poor")]
    handles.append(Line2D([], [], marker="o", ls="", ms=8, mfc=SURFACE, mec=INK_2, mew=1.8,
                          label=f"Fewer than {MIN_SAMPLES_FOR_GEOMEAN} samples that season"))
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(0, -0.2), ncols=4, fontsize=8)
    _brand_footer(fig, logo_height_in=0.32)
    return fig


def site_timeseries_pdf(samples, sites, out: Path, png_dir: Path | None = None) -> dict[int, str]:
    """All sites in one PDF (out), plus one small PDF per site in out.parent/sites/.
    Returns {site_id: relative path of that site's PDF}."""
    order = sites.sort_values(["waterbody", "site"])
    site_dir = out.parent / "sites"
    site_dir.mkdir(exist_ok=True)
    made: dict[int, str] = {}
    with PdfPages(out) as pdf:
        for _, srow in order.iterrows():
            allg = samples[samples.site_id == srow.site_id]
            g = allg[allg.in_season]
            if g.empty:
                continue
            seasons = sorted(int(y) for y in g.year.unique())
            gx = g.assign(x=_season_x(g.date, seasons))

            heading, place = popup_heading(srow.site, srow.waterbody, srow.borough, srow.neighborhood)
            fig1 = site_geomean_chart(g, srow, heading, place)        # page 1: simple line

            fig, ax = plt.subplots(figsize=(11, 4.8))                  # page 2: every sample
            _mpn_axis(ax)
            ax.set_xlim(0, len(seasons))
            _quality_bands(ax)
            for i in range(1, len(seasons)):
                ax.axvline(i, color=GRID, lw=0.8, zorder=1)
            dry, wet, unk = gx[gx.wet == 0], gx[gx.wet == 1], gx[gx.wet.isna()]
            ax.scatter(dry.x, dry.mpn, s=20, color=SERIES, lw=0, alpha=0.85, zorder=3,
                       label=f"Dry weather (<{WET_WEATHER_INCHES}\" rain)")
            ax.scatter(wet.x, wet.mpn, s=20, color=SERIES_2, lw=0, alpha=0.85, zorder=3,
                       label=f"Wet weather (≥{WET_WEATHER_INCHES}\" rain)")
            if len(unk):
                ax.scatter(unk.x, unk.mpn, s=20, facecolor="none", edgecolor=INK_2, lw=0.9,
                           zorder=3, label="Rainfall not entered yet")
            ticklabels = []
            for i, y in enumerate(seasons):
                gy = gx[gx.year == y]
                gm = geomean(gy.mpn)
                ax.hlines(gm, i + 0.05, i + 0.95, color=INK, lw=2, zorder=4, capstyle="round")
                ticklabels.append(f"{y}\n{gm:,.0f}")
            ax.set_xticks(np.arange(len(seasons)) + 0.5)
            ax.set_xticklabels(ticklabels, fontsize=8.5 if len(seasons) < 12 else 7.8)
            ax.tick_params(axis="x", length=0)
            ax.grid(axis="x", visible=False)
            ax.set_xlabel("Season  (number underneath = that season's geometric mean)", fontsize=8.5)
            ax.set_ylabel("Enterococcus, MPN / 100 ml (log)")
            ax.set_title(f"{heading}: every sample", pad=28)
            st = summarize(g)
            cls = f" · DEC class {srow.dec_class}" if srow.dec_class else ""
            off = len(allg) - len(g)
            off_txt = f" · {off} off-season sample{'s' if off != 1 else ''} not shown" if off else ""
            _subtitle(ax, f"{place} · Site ID {srow.site_id}{cls} · {st.n:,} samples · overall "
                          f"geomean {st.geomean:,.0f} · {st.pct_over_limit:.0f}% over "
                          f"{SINGLE_SAMPLE_LIMIT}{off_txt}")
            handles, labels_ = ax.get_legend_handles_labels()
            handles.append(Line2D([], [], color=INK, lw=2))
            labels_.append("Season geometric mean")
            ax.legend(handles, labels_, loc="upper left", bbox_to_anchor=(0, -0.2), ncols=4,
                      fontsize=8)
            _brand_footer(fig, logo_height_in=0.32)
            rel = site_pdf_name(srow.site_id)
            with PdfPages(out.parent / rel) as one:       # the site's own two-page PDF
                for f_ in (fig1, fig):
                    one.savefig(f_)
                    pdf.savefig(f_)
            made[int(srow.site_id)] = rel
            if png_dir is not None:
                fn = re.sub(r"[^A-Za-z0-9]+", "_", f"{srow.site_id:03d}_{srow.site}").strip("_")
                fig1.savefig(png_dir / f"{fn}_geomean.png")
                fig.savefig(png_dir / f"{fn}_samples.png")
            plt.close(fig1)
            plt.close(fig)
    return made


# =============================================================================
# WEB PAGES (map + charts page), filled in from the templates/ folder
# =============================================================================
# The look, layout and wording of the web pages live in templates/ (map.html,
# charts.html, cwqt.css), so they can be edited without touching this script.
# The script only fills in the data. Placeholders in the templates look like
# {{NAME}}.

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# SWIM logo: the same image used in the header of swimmablenyc.org. To stop relying on
# that address, save the logo as templates/assets/swim-logo.png; it is then used instead.
LOGO_URL = ("https://static.wixstatic.com/media/875d16_e74e0d2d48474f5d9571e24e7277fb3d~mv2.png/"
            "v1/fill/w_541,h_120,al_c,q_85,usm_0.66_1.00_0.01,enc_avif,quality_auto/"
            "NEW-SWIM-Logo%20email%20res.png")


def _template(name: str) -> str:
    path = TEMPLATE_DIR / name
    if not path.exists():
        sys.exit(f"Missing web template {path}. Keep the templates/ folder next to cwqt_visualize.py.")
    return path.read_text(encoding="utf-8")


def _fill(text: str, values: dict) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def _warn_unfilled(text: str, name: str):
    left = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", text)))
    if left:
        print(f"  note: {name} has placeholders the script doesn't fill: {', '.join(left)}")


def copy_web_assets(out: Path) -> str:
    """Copy cwqt.css and templates/assets/ next to the pages, make the favicons,
    and return the logo address to use in the banner."""
    import shutil
    from PIL import Image
    shutil.copyfile(TEMPLATE_DIR / "cwqt.css", out / "cwqt.css")
    assets = TEMPLATE_DIR / "assets"
    if not assets.is_dir():
        return LOGO_URL
    shutil.copytree(assets, out / "assets", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("fonts", ".DS_Store"))
    make_favicons(assets / "swim-emblem.png", out)
    logo = assets / "swim-logo.png"
    if not logo.exists():
        return LOGO_URL
    # Trim the logo's empty transparent margins so it shows at full size in the banner
    im = Image.open(logo).convert("RGBA")
    box = im.getbbox()
    if box:
        im = im.crop(box)
        im.thumbnail((1200, 240))
        im.save(out / "assets" / "swim-logo.png", optimize=True)
    return "assets/swim-logo.png"


def make_favicons(emblem: Path, out: Path):
    """Browser-tab icons from any square-ish PNG (here the SWIM swimmer emblem):
    favicon.ico (16/32/48 px, for older browsers), favicon-32.png (modern browsers) and
    apple-touch-icon.png (180 px, phone home screens; white background as iOS requires)."""
    if not emblem.exists():
        return
    from PIL import Image
    im = Image.open(emblem).convert("RGBA")
    im = im.crop(im.getbbox() or (0, 0, *im.size))
    side = int(max(im.size) * 1.04)                      # square, small margin
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
    sq.resize((32, 32), Image.LANCZOS).save(out / "favicon-32.png", optimize=True)
    (out / "assets").mkdir(exist_ok=True)
    sq.resize((192, 192), Image.LANCZOS).save(out / "assets" / "swim-emblem-192.png", optimize=True)
    sq.save(out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    touch = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    pad = int(side * 0.12)
    touch.paste(sq.resize((side - 2 * pad, side - 2 * pad), Image.LANCZOS), (pad, pad),
                sq.resize((side - 2 * pad, side - 2 * pad), Image.LANCZOS))
    touch.convert("RGB").resize((180, 180), Image.LANCZOS).save(out / "apple-touch-icon.png", optimize=True)


# ---- Popup headings ---------------------------------------------------------

WATER_WORDS = re.compile(r"\b(River|Bay|Harbor|Creek|Kill|Channel|Canal|Lake|Basin|Inlet|Sound|"
                         r"Cove|Lagoon|Pond|Narrows|Estuary)$", re.I)


def popup_heading(site: str, waterbody: str, borough: str, town: str) -> tuple[str, str]:
    """('Rutgers Boathouse, Raritan River', 'New Brunswick · New Jersey') from the sheet's
    'Raritan River, Rutgers Boathouse, New Brunswick NJ'. Rules:
      * the location comes first, then the water name. The water name is the one the site
        name starts with (usually the Waterbody column, e.g. 'Raritan River', but sometimes a
        different one, e.g. 'Hudson River, Dyckman Marina' -> 'Dyckman Marina, Hudson River');
      * for New Jersey sites, a trailing ', <town> NJ' is dropped (the town is on line 2);
      * a name that is just the waterbody, or already contains it, stays as is; any other
        name gets the waterbody added at the end.
    Line 2 is '<town> · New Jersey' for NJ sites and '<borough> · New York' otherwise."""
    name = str(site).strip()
    wb = str(waterbody).strip()
    town = "" if pd.isna(town) else str(town).strip()
    borough = "" if pd.isna(borough) else str(borough).strip()
    nj = borough.lower() == "new jersey"
    cap = lambda t: t[:1].upper() + t[1:]
    nj_tail = re.compile(r",?\s*NJ\.?$", re.I)

    def place():
        return (f"{town} · New Jersey" if town else "New Jersey") if nj else \
               (f"{borough} · New York" if borough else "New York")

    first, _, after = name.partition(",")
    after = after.strip()
    if not wb or wb == "Unknown" or name.lower() == wb.lower():
        return name, place()
    if first.strip().lower() == wb.lower() or (after and WATER_WORDS.search(first.strip())):
        water, rest = first.strip(), after
    elif wb.lower() in name.lower():
        return cap(name), place()
    else:
        return f"{cap(name)}, {wb}", place()
    if nj:
        if not town:   # no town on the Information tab: take it from 'Hackensack, NJ'
            m = re.match(r"^(?:.*,\s*)?([^,\s][^,]*?),?\s*NJ\.?$", rest, re.I)
            if m:
                town = m.group(1).strip()
        trimmed = rest
        if town:
            trimmed = re.sub(r",?\s*" + re.escape(town) + r",?\s*NJ\.?$", "", trimmed, flags=re.I)
        trimmed = nj_tail.sub("", trimmed).strip(" ,")
        rest = trimmed or nj_tail.sub("", rest).strip(" ,") or rest
    return f"{cap(rest)}, {water}", place()


# ---- Extra map layers from data/ ------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent / "data"


# Per-site links to each site's raw results sheet ("See raw data" in the map popups).
# Any CSV in data/ matching this name, with a site_id column and a link column.
SITE_LINKS_FILE = "CWQT_Sites_GoogleMyMap*.csv"
SITE_LINKS_COLUMN = "results_sheet_url"


def load_site_links() -> dict[int, str]:
    """{site_id: raw-results link} from data/CWQT_Sites_GoogleMyMap.csv (newest match wins)."""
    matches = sorted(DATA_DIR.glob(SITE_LINKS_FILE))
    if not matches:
        return {}
    links = {}
    with open(matches[-1], newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            sid_, url = (r.get("site_id") or "").strip(), (r.get(SITE_LINKS_COLUMN) or "").strip()
            if re.fullmatch(r"\d+", sid_) and url.startswith("https://"):
                links[int(sid_)] = url
    print(f"  raw-data links for {len(links)} sites from {matches[-1].name}")
    return links


def _simplify(points, tol):
    """Douglas-Peucker line simplification (tol in degrees; 1e-5 is about 1 m)."""
    pts = np.asarray(points, dtype=float)
    if len(pts) < 5:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        seg = pts[b] - pts[a]
        rel = pts[a + 1:b] - pts[a]
        norm = np.hypot(*seg)
        d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / norm if norm else np.hypot(rel[:, 0], rel[:, 1])
        i = int(np.argmax(d))
        if d[i] > tol:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    out = pts[keep]
    return out if len(out) >= 4 else pts


def _slim_geometry(g, tol=1e-5, nd=5):
    t, c = g["type"], g["coordinates"]
    rnd = lambda arr: [[round(float(x), nd), round(float(y), nd)] for x, y in arr]
    if t == "Point":
        return {"type": t, "coordinates": [round(float(c[0]), nd), round(float(c[1]), nd)]}
    if t == "Polygon":
        return {"type": t, "coordinates": [rnd(_simplify(r, tol)) for r in c]}
    if t == "MultiPolygon":
        return {"type": t, "coordinates": [[rnd(_simplify(r, tol)) for r in poly] for poly in c]}
    if t == "LineString":
        return {"type": t, "coordinates": rnd(_simplify(c, tol))}
    if t == "MultiLineString":
        return {"type": t, "coordinates": [rnd(_simplify(l, tol)) for l in c]}
    return g


def _clean_value(v):
    if v is None:
        return None
    if isinstance(v, float):
        return int(v) if v.is_integer() else round(v, 2)
    v = str(v).strip()
    if v in ("", "None", "N/A", "nan", "NaN", "null"):
        return None
    if re.fullmatch(r"-?\d+\.0", v):          # '190845.0' -> '190845'
        v = v[:-2]
    return v


def _gi_built_date(raw, built, today):
    """The construction date of a feature, or None if it's missing or implausible
    (before built_date's min_year, e.g. the 1899-12-30 "no date" placeholder, or in the future)."""
    d = next((raw.get(k) for k in built["fields"] if raw.get(k)), None)
    d = pd.to_datetime(d, errors="coerce") if d else pd.NaT
    return d if pd.notna(d) and built.get("min_year", 0) <= d.year and d <= today else None


def _status_group(status: str) -> str:
    """The status group DEP derives from a detailed status (true in every row of the Point Layer,
    Porous Pavement and Medians files)."""
    if status.startswith("Constructed"):
        return "Constructed"
    return "In Construction" if status == "In Construction" else "Final Design"


def _gi_done(raw, complete, built, today):
    """Is a green infrastructure feature finished? Returns (done, status text for the popup).
    A feature is finished when its status starts with complete["starts_with"]. If the file also
    has a status group (complete["group_field"]) that disagrees, as happens in Regulated Projects,
    the construction date breaks the tie: finished only with a valid construction date."""
    status = str(raw.get(complete["field"]) or "").strip()
    done = status.startswith(complete["starts_with"])
    group = str(raw.get(complete.get("group_field", "")) or "").strip()
    if status and group and _status_group(status) != group:
        if (group == "Constructed") != done:
            done = bool(built) and _gi_built_date(raw, built, today) is not None
        return done, f"{status} (status group: {group})"
    return done, status or None


def build_data_layers(out: Path) -> list[dict]:
    """Turn the files listed in data/layers.json into small GeoJSON files in out/layers/
    (only the fields the popups use, coordinates rounded to ~1 m, outlines simplified).
    Returns what the map needs to know about each layer."""
    cfg_path = DATA_DIR / "layers.json"
    if not cfg_path.exists():
        return []
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    lists = {k: set(v) for k, v in cfg.get("lists", {}).items() if isinstance(v, list)}
    today = pd.Timestamp.now().normalize()
    layer_dir = out / "layers"
    layer_dir.mkdir(parents=True, exist_ok=True)
    metas = []
    for L in cfg.get("layers", []):
        files, file_label = [], {}
        for pattern in L["files"]:
            matches = sorted(DATA_DIR.glob(pattern))
            if matches:
                files.append(matches[-1])          # newest dated download wins
                file_label[matches[-1]] = L.get("file_labels", {}).get(pattern)
        if not files:
            print(f"  note: no file in data/ matches layer '{L['name']}' ({', '.join(L['files'])}); skipped")
            continue
        cats = L.get("categories")
        values = L.get("values", {})
        complete, built = L.get("complete"), L.get("built_date")
        only = L.get("only")              # "complete" or "pipeline": keep just those features
        # Fields made here rather than read from the file: "_source" (the file's label),
        # "_done" (1 = finished, 0 = in design/construction), "_built" (month and year finished)
        # and "_status" (the status, noting the status group when the two disagree)
        made = ["_source"] if L.get("file_labels") else []
        made += ["_done"] if complete and not only else []
        keep = [k for k in [L.get("title"), L.get("subtitle"), L.get("contacts")] + list(L.get("fields", {}))
                + made if k]
        keep = list(dict.fromkeys(keep))
        if not built:
            keep = [k for k in keep if k != "_built"]
        where = L.get("where")
        unlisted = {}
        if cats and cats["field"] not in keep:
            keep.append(cats["field"])
        # Compact format: every distinct value is stored once in "s" and features refer to it
        # by number; points are just [lon, lat]. (The map turns this back into GeoJSON.)
        strings, index = [], {}

        def sid(v):
            v = f"{v:,}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
            if v not in index:
                index[v] = len(strings)
                strings.append(v)
            return index[v]

        rows, kinds = [], set()
        for f in files:
            for feat in json.loads(f.read_text(encoding="utf-8")).get("features", []):
                g = feat.get("geometry")
                if not g or not g.get("coordinates"):
                    continue
                raw = feat.get("properties") or {}
                if where:   # e.g. only labs whose status is "Active"
                    wv = str(_clean_value(raw.get(where["field"])) or "")
                    if "equals" in where and wv != str(where["equals"]):
                        continue
                    if "not_equals" in where and wv == str(where["not_equals"]):
                        continue
                    if "in_list" in where and wv not in lists.get(where["in_list"], set()):
                        continue
                    if "not_in_list" in where and wv in lists.get(where["not_in_list"], set()):
                        continue
                    if "note_unless_in" in where and wv not in lists.get(where["note_unless_in"], set()):
                        unlisted[wv or "(blank)"] = unlisted.get(wv or "(blank)", 0) + 1
                raw = dict(raw)
                raw["_source"] = file_label.get(f)
                if complete:
                    done, raw["_status"] = _gi_done(raw, complete, built, today)
                    if only and (only == "complete") != done:
                        continue
                    raw["_done"] = "1" if done else "0"
                    if built and done:   # month and year finished, only for finished features
                        d = _gi_built_date(raw, built, today)
                        if d is not None:
                            raw["_built"] = f"{d:%B %Y}"
                vals = []
                for k in keep:
                    v = _clean_value(raw.get(k))
                    vals.append(-1 if v is None else sid(values.get(k, {}).get(str(v), v)))
                kinds.add("point" if g["type"] == "Point" else "shape")
                gs = _slim_geometry(g)
                row = [gs["coordinates"] if g["type"] == "Point" else gs, vals]
                if cats:
                    rv = str(_clean_value(raw.get(cats["field"])))
                    row.append(next((i for i, c in enumerate(cats["classes"])
                                     if rv in c.get("values", []) or rv in lists.get(c.get("list"), ())),
                                    len(cats["classes"])))
                rows.append(row)
        if unlisted:
            print(f"  note: types not in the '{where['note_unless_in']}' list, drawn as "
                  f"'{L['name']}' or its pipeline (check data/layers.json): "
                  + ", ".join(f"{k} ({n})" for k, n in sorted(unlisted.items())))
        # Points stay as plain [lon, lat] even when a layer also has shapes; the map draws only the
        # points on screen and every shape.
        points = kinds == {"point"}
        kind = "point" if points else "shape" if kinds == {"shape"} else "mixed"
        path = layer_dir / f"{L['id']}.json"
        path.write_text(json.dumps({"k": keep, "s": strings, "p": points, "f": rows},
                                   separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        feats = rows
        metas.append({
            "id": L["id"], "group": L.get("group", "Other layers"), "name": L["name"],
            "url": f"layers/{L['id']}.json", "title": L.get("title"),
            "titlePrefix": L.get("titlePrefix", ""),
            "subtitle": L.get("subtitle"), "contacts": L.get("contacts"), "hover": bool(L.get("hover")),
            "fields": [[k, lab] for k, lab in L.get("fields", {}).items()],
            "style": L.get("style", {}), "minZoom": L.get("minZoom", 0), "on": bool(L.get("on")),
            "kind": kind, "complete": bool(complete) and not only, "pipeline": only == "pipeline",
            "categories": ({"label": cats.get("label", ""),
                            "classes": [{"label": c["label"], "color": c["color"]} for c in cats["classes"]]
                                       + [cats.get("other", {"label": "Other", "color": "#8a8f99"})]}
                           if cats else None),
        })
        print(f"  layer '{L['name']}': {len(feats):,} features from {', '.join(x.name for x in files)} "
              f"-> {path.stat().st_size / 1e6:.1f} MB")
    return metas


def _num(v, nd=1):
    """JSON-safe number: NaN/None -> None, otherwise rounded."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    f = round(f, nd)
    return int(f) if nd == 0 or f.is_integer() else f


def _stat_rows(stats: pd.DataFrame, site_ids: set) -> list:
    """One compact row per site:
    [site_id, geomean, n, pct_over, gm_dry, n_dry, gm_wet, n_wet, first, last, last_mpn]"""
    rows = []
    for _, st in stats.sort_values("geomean").iterrows():
        if st.site_id not in site_ids:
            continue
        rows.append([int(st.site_id), _num(st.geomean), int(st.n), _num(st.pct_over_limit, 0),
                     _num(st.geomean_dry), int(st.n_dry), _num(st.geomean_wet), int(st.n_wet),
                     f"{st['first']:%Y-%m-%d}", f"{st['last']:%Y-%m-%d}", _num(st.last_mpn)])
    return rows


def build_map(season, sites, site_all, site_year, win, pdfs: dict, logo: str, out: Path,
              extra_layers: list | None = None):
    """Write the map page (index.html) from templates/map.html."""
    have = sites[sites.site_id.isin(season.site_id)].dropna(subset=["lat", "lon"])
    ids = set(have.site_id)
    y0, y1 = int(season.year.min()), int(season.year.max())

    # Order of the choices under Layers > CWQT water quality. The first one is shown when
    # the map opens; "earlier": True puts a season under the nested "Earlier seasons" list.
    # "legend" is the part in brackets in the legend title, e.g. "Enterococcus Bacteria Levels (2026)".
    seasons = sorted((int(y) for y in site_year.year.unique()), reverse=True)
    layers = [{"name": f"{seasons[0]} season", "label": f"{seasons[0]} season", "legend": str(seasons[0]),
               "rows": _stat_rows(site_year[site_year.year == seasons[0]], ids)}]
    if win is not None and len(win):
        s0, s1 = win.window_start.iloc[0], win.window_end.iloc[0]
        layers.append({"name": f"Latest 30 days ({s0:%b %-d}–{s1:%b %-d})",
                       "label": f"30 days, {s0:%b %-d}–{s1:%b %-d, %Y}", "legend": "Latest 30 Days",
                       "rows": _stat_rows(win, ids)})
    layers.append({"name": f"All seasons ({y0}–{y1})", "label": f"all seasons {y0}–{y1}", "legend": "All Seasons",
                   "rows": _stat_rows(site_all, ids)})
    for y in seasons[1:]:
        layers.append({"name": f"{y} season", "label": f"{y} season", "legend": str(y), "earlier": True,
                       "rows": _stat_rows(site_year[site_year.year == y], ids)})

    data = {
        # site_id -> [popup title, place line, DEC class, lat, lon]
        "sites": {int(r.site_id): [*popup_heading(r.site, r.waterbody, r.borough, r.neighborhood),
                                   r.dec_class, round(float(r.lat), 5), round(float(r.lon), 5)]
                  for r in have.itertuples()},
        "extraLayers": extra_layers or [],
        # site_id -> [[season, geomean], ...] for the popup trend chart
        "trend": {int(sid): [[int(y), _num(gm)] for y, gm in zip(g.year, g.geomean) if np.isfinite(gm)]
                  for sid, g in site_year.sort_values("year").groupby("site_id")},
        "overall": {int(r.site_id): _num(r.geomean) for r in site_all.itertuples()},
        "pdfs": {sid: rel for sid, rel in pdfs.items() if sid in ids},
        "raw": {sid: url for sid, url in load_site_links().items() if sid in ids},
        "layers": layers,
        "years": [y0, y1],
        "cfg": {"good": GOOD_MAX, "adv": ADVISORY_MAX, "limit": SINGLE_SAMPLE_LIMIT,
                "minN": MIN_SAMPLES_FOR_GEOMEAN, "floor": MPN_FLOOR,
                "crit": {k: list(v) for k, v in DEC_CRITERIA.items()},
                "colors": CATEGORY_COLORS, "labels": CATEGORY_LABELS,
                "series": SERIES, "ink2": INK_2},
        "bounds": [[float(have.lat.min()), float(have.lon.min())],
                   [float(have.lat.max()), float(have.lon.max())]],
        "updated": f"Data through {season.date.max():%b %-d, %Y}",
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = _fill(_template("map.html"), {"LOGO_URL": html.escape(logo)})
    _warn_unfilled(page.replace("{{DATA}}", ""), "map.html")
    out.write_text(page.replace("{{DATA}}", payload, 1), encoding="utf-8")


def build_charts_page(season, sites, win, pdfs: dict, logo: str, out: Path,
                      gi: pd.DataFrame | None = None, gi_downloaded=None):
    """Write the charts page (charts.html) from templates/charts.html."""
    have = sites[sites.site_id.isin(season.site_id)]
    y0, y1 = int(season.year.min()), int(season.year.max())
    first_recent = y1 - RECENT_SEASONS + 1

    # Per-site PDF list, grouped by waterbody
    parts = []
    listed = sites[sites.site_id.isin(pdfs.keys())].sort_values(["waterbody", "site"])
    for wb, g in listed.groupby("waterbody", sort=True):
        items = "".join(
            f'<li><a href="{pdfs[int(r.site_id)]}" target="_blank" rel="noopener">'
            f'{html.escape(short_name(r.site, wb))}</a></li>' for r in g.itertuples())
        parts.append(f'<details><summary>{html.escape(wb)} <span class="muted">({len(g)} '
                     f'site{"s" if len(g) != 1 else ""})</span></summary><ul>{items}</ul></details>')

    window = ""
    if win is not None and len(win):
        window = f"{win.window_start.iloc[0]:%b %-d} – {win.window_end.iloc[0]:%b %-d, %Y}"
    values = {
        "LOGO_URL": html.escape(logo),
        "DATA_THROUGH": f"Data through {season.date.max():%b %-d, %Y}",
        "N_SITES": f"{len(have)}",
        "N_SAMPLES": f"{len(season):,}",
        "SEASON_RANGE": f"{y0}–{y1}",
        "RECENT_RANGE": f"{first_recent}–{y1}",
        "RECENT_SEASONS": RECENT_SEASONS,
        "WINDOW": window,
        "GOOD_MAX": GOOD_MAX,
        "ADVISORY_MAX": ADVISORY_MAX,
        "LIMIT": SINGLE_SAMPLE_LIMIT,
        "MIN_N": MIN_SAMPLES_FOR_GEOMEAN,
        "BUILT": pd.Timestamp.now(tz="America/New_York").strftime("%b %-d, %Y"),
        "SITE_LIST": "\n".join(parts),
        "GI_BUILT": f"{int(gi.finished.sum()):,}" if gi is not None and len(gi) else "—",
        "GI_PIPELINE": f"{int((~gi.finished).sum()):,}" if gi is not None and len(gi) else "—",
        "GI_DOWNLOADED": f"{gi_downloaded:%B %-d, %Y}" if gi_downloaded is not None else "the latest download",
    }
    page = _fill(_template("charts.html"), values)
    _warn_unfilled(page, "charts.html")
    out.write_text(page, encoding="utf-8")


# =============================================================================
# MAIN
# =============================================================================

def download_sheet(sheet_id: str, dest: Path) -> Path:
    """Download the Google Sheet as .xlsx (formulas' last calculated values included).
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    headers = {"User-Agent": "cwqt-visualize"}
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if key:
        from google.auth.transport.requests import Request as GRequest
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            json.loads(key), scopes=["https://www.googleapis.com/auth/drive.readonly"])
        creds.refresh(GRequest())
        headers["Authorization"] = f"Bearer {creds.token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=120) as r:
            body = r.read()
    except (HTTPError, URLError) as e:
        sys.exit(f"Could not download the Google Sheet ({e}). Check the sheet ID and sharing.")
    if not body.startswith(b"PK"):  # .xlsx files are zip archives; anything else is a login page
        sys.exit("Google returned a web page instead of a spreadsheet. The sheet is probably not "
                 "shared with link viewers or with the service account.")
    dest.write_bytes(body)
    print(f"Downloaded sheet ({len(body) / 1e6:.1f} MB) to {dest}")
    return dest


def write_qa(qa: list[QAIssue], checks: list[str], out: Path):
    lines = ["CWQT data quality report", "=" * 24, ""]
    lines += ["Cross-check against the workbook's own GeoMean tabs:"] + [f"  {c}" for c in checks] + [""]
    for cat in dict.fromkeys(q.category for q in qa):
        lines.append(cat)
        lines.append("-" * len(cat))
        lines += [f"  - {q.detail}" for q in qa if q.category == cat]
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def _r(df: pd.DataFrame) -> pd.DataFrame:
    """Round numeric columns for CSV output; format dates as YYYY-MM-DD."""
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.strftime("%Y-%m-%d")
        elif pd.api.types.is_float_dtype(df[c]):
            df[c] = df[c].round(2)
    return df


def main(argv=None):
    ap = argparse.ArgumentParser(description="Maps and charts from the CWQT master data sheet.")
    ap.add_argument("xlsx", type=Path, nargs="?",
                    help="CWQT master data workbook (.xlsx). Leave out to download it with --sheet-id.")
    ap.add_argument("--sheet-id", default=os.environ.get("CWQT_SHEET_ID"),
                    help="Google Sheet ID to download (the long code in the sheet's URL). "
                         "Can also be set with the CWQT_SHEET_ID environment variable.")
    ap.add_argument("--out", type=Path, default=Path("cwqt_outputs"), help="output folder")
    ap.add_argument("--window-end", type=str, default=None,
                    help="last day of the 30-day window (YYYY-MM-DD); default = latest sample date")
    ap.add_argument("--average-splits", action="store_true",
                    help="average lab/field split results recorded in Notes (per the FAQ). "
                         "Off by default so numbers match the GeoMean tabs.")
    ap.add_argument("--site-pngs", action="store_true",
                    help="also save each site's two charts as PNGs (in addition to the PDF)")
    ap.add_argument("--no-map", action="store_true", help="skip the web pages (map and charts page)")
    args = ap.parse_args(argv)

    out = args.out
    tables = out / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    _style()
    if args.xlsx is None:
        if not args.sheet_id:
            ap.error("give an .xlsx file or a --sheet-id to download")
        args.xlsx = download_sheet(args.sheet_id, out / "CWQT_Master_Data_Sheet.xlsx")

    print(f"Reading {args.xlsx} …")
    qa: list[QAIssue] = []
    sites = load_sites(args.xlsx, qa)
    samples = load_samples(args.xlsx, sites, qa, average_splits=args.average_splits)
    print(f"  {len(samples):,} samples at {samples.site_id.nunique()} sites, "
          f"{samples.date.min():%b %Y} – {samples.date.max():%b %Y}")
    checks = [] if args.average_splits else verify_against_workbook(args.xlsx, samples)
    for c in checks:
        print("  " + c)

    global _footer_note
    _footer_note = f"Data through {samples.date.max():%b %-d, %Y}"
    season = samples[samples.in_season]          # regular-season samples only
    print(f"  {len(season):,} regular-season samples ({SEASON_START[0]}/{SEASON_START[1]}–"
          f"{SEASON_END[0]}/{SEASON_END[1]}) used for seasonal statistics")
    site_all = site_stats(season)
    site_year = site_stats(season, by_year=True)
    end = pd.Timestamp(args.window_end) if args.window_end else samples.date.max()
    win = rolling_window_stats(samples, end, 30)

    meta = sites[["site_id", "site", "waterbody", "borough", "dec_class", "lat", "lon"]]
    _r(meta.merge(site_all, on="site_id")).to_csv(tables / "site_summary_all_seasons.csv", index=False)
    _r(meta.merge(site_year, on="site_id")).to_csv(tables / "site_summary_by_season.csv", index=False)
    _r(meta.merge(win, on="site_id")).to_csv(tables / "latest_30_day_window.csv", index=False)

    steps = [
        ("1_program_trend.png", lambda p: chart_program_trend(season, site_year, p, tables)),
        ("2_waterbody_comparison.png", lambda p: chart_waterbodies(season, p, tables)),
        ("3_rainfall_effect.png", lambda p: chart_rainfall(season, p, tables)),
        ("4_rain_recovery.png", lambda p: chart_rain_recovery(season, p, tables)),
        ("5_green_infrastructure.png", lambda p: chart_green_infrastructure(gi, gi_downloaded, p, tables)),
        ("6_latest_30_days_by_waterbody.png", lambda p: chart_latest_window(samples, win, p, tables)),
        ("7_site_by_season_heatmap.png", lambda p: chart_site_heatmap(site_year, sites, p, tables)),
    ]
    gi, gi_downloaded = load_gi()
    for name, fn in steps:
        if name.startswith("5_green") and gi.empty:
            print(f"  note: no green infrastructure files found in data/; skipped {name}")
            continue
        fn(out / name)
        print(f"  wrote {out / name}")

    png_dir = None
    if args.site_pngs:
        png_dir = out / "site_pngs"
        png_dir.mkdir(exist_ok=True)
    pdfs = site_timeseries_pdf(samples, sites, out / "site_timeseries.pdf", png_dir)
    print(f"  wrote {out / 'site_timeseries.pdf'} and {len(pdfs)} single-site PDFs in {out / 'sites'}")

    if not args.no_map:
        logo = copy_web_assets(out)
        extra = build_data_layers(out)
        build_map(season, sites, site_all, site_year, win, pdfs, logo, out / "index.html", extra)
        print(f"  wrote {out / 'index.html'} (map, {(out / 'index.html').stat().st_size / 1e3:,.0f} KB)")
        build_charts_page(season, sites, win, pdfs, logo, out / "charts.html", gi, gi_downloaded)
        print(f"  wrote {out / 'charts.html'}")

    # Small machine-readable summary (used by the automatic update to detect new data)
    (out / "status.json").write_text(json.dumps({
        "data_through": f"{samples.date.max():%Y-%m-%d}",
        "samples": int(len(samples)),
        "sites": int(samples.site_id.nunique()),
    }, indent=2) + "\n", encoding="utf-8")

    write_qa(qa, checks, out / "data_quality_report.txt")
    print(f"  wrote {out / 'data_quality_report.txt'} ({len(qa)} items)")
    print("Done.")


if __name__ == "__main__":
    main()
