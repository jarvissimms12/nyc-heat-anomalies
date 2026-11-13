"""
etl.py - Extract, validate and load NYC 311 heat / hot-water complaints.

Source: NYC Open Data, "311 Service Requests from 2010 to Present" (erm2-nwe9),
        queried through the Socrata SODA API.

Usage:
    python etl.py --start 2019-10-01 --end 2025-06-01 --db heat.db
    python etl.py --start 2019-10-01 --end 2025-06-01 --db heat.db --app-token $SOCRATA_TOKEN

Each reliability check writes one row to the `data_quality_log` table, so
anyone reviewing the analysis can see exactly what was removed or flagged and why.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

API_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
COMPLAINT_TYPE = "HEAT/HOT WATER"
PAGE_SIZE = 50_000
FIELDS = [
    "unique_key", "created_date", "closed_date", "complaint_type", "descriptor",
    "incident_address", "borough", "incident_zip", "bbl", "latitude", "longitude",
    "status",
]
# Generous bounding box around the five boroughs.
NYC_LAT = (40.49, 40.92)
NYC_LON = (-74.27, -73.68)

SQL_DIR = Path(__file__).parent / "sql"
log = logging.getLogger("etl")


# --------------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------------- #
def extract(start: str, end: str, app_token: str | None = None,
            max_retries: int = 5) -> pd.DataFrame:
    """Page through the SODA API and return every matching record."""
    where = (
        f"complaint_type = '{COMPLAINT_TYPE}' "
        f"AND created_date >= '{start}T00:00:00' AND created_date < '{end}T00:00:00'"
    )
    headers = {"X-App-Token": app_token} if app_token else {}
    pages, offset = [], 0

    while True:
        params = {
            "$select": ", ".join(FIELDS),
            "$where": where,
            "$order": "unique_key",          # stable ordering is required for paging
            "$limit": PAGE_SIZE,
            "$offset": offset,
        }
        for attempt in range(max_retries):
            try:
                resp = requests.get(API_URL, params=params, headers=headers, timeout=120)
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                wait = 2 ** attempt
                log.warning("Request failed (%s); retrying in %ss", exc, wait)
                time.sleep(wait)
        else:
            raise RuntimeError(f"Giving up after {max_retries} attempts at offset {offset}")

        batch = resp.json()
        if not batch:
            break
        pages.append(pd.DataFrame(batch))
        offset += len(batch)
        log.info("Fetched %s rows so far", f"{offset:,}")

    if not pages:
        return pd.DataFrame(columns=FIELDS)
    return pd.concat(pages, ignore_index=True).reindex(columns=FIELDS)


# --------------------------------------------------------------------------- #
# Transform / validate
# --------------------------------------------------------------------------- #
def is_valid_bbl(bbl: pd.Series) -> pd.Series:
    """A valid BBL is 10 digits: borough (1-5) + 5-digit block + 4-digit lot, block > 0."""
    s = bbl.astype("string").str.replace(r"\.0$", "", regex=True).str.strip()
    shape_ok = s.str.fullmatch(r"[1-5]\d{9}").fillna(False)
    block_ok = s.str.slice(1, 6).ne("00000").fillna(False)
    return (shape_ok & block_ok).astype(bool)


def validate(df: pd.DataFrame, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Apply reliability checks. Returns (clean_df, quality_report).

    Records are DROPPED only when they cannot be used for a building-level
    analysis. Problems that do not affect that use are FLAGGED and kept, so
    information is not thrown away without a reason.
    """
    report: list[dict] = []

    def note(check: str, n: int, action: str) -> None:
        report.append({"check_name": check, "n_rows": int(n), "action": action})
        log.info("%-38s %8s rows  -> %s", check, f"{n:,}", action)

    df = df.copy()
    note("rows_extracted", len(df), "none")

    # 1. Duplicate service-request IDs (can occur across API pages if the source
    #    is updated mid-pull).
    dupes = df.duplicated("unique_key", keep="first")
    note("duplicate_unique_key", dupes.sum(), "dropped")
    df = df[~dupes]

    # 2. Unparseable or missing creation timestamps.
    df["created_date"] = pd.to_datetime(df["created_date"], errors="coerce")
    df["closed_date"] = pd.to_datetime(df["closed_date"], errors="coerce")
    bad_created = df["created_date"].isna()
    note("missing_created_date", bad_created.sum(), "dropped")
    df = df[~bad_created]

    # 3. Records outside the requested window (guards against API filter drift).
    out_of_window = (df["created_date"] < pd.Timestamp(start)) | (df["created_date"] >= pd.Timestamp(end))
    note("created_outside_window", out_of_window.sum(), "dropped")
    df = df[~out_of_window]

    # 4. Closed before created - the resolution time is impossible, but the
    #    complaint itself is still real. Null the closed date and keep the row.
    bad_order = df["closed_date"].notna() & (df["closed_date"] < df["created_date"])
    note("closed_before_created", bad_order.sum(), "closed_date set to NULL")
    df.loc[bad_order, "closed_date"] = pd.NaT

    # 5. Missing / malformed BBL - cannot be attributed to a building.
    bbl_ok = is_valid_bbl(df["bbl"])
    note("invalid_or_missing_bbl", (~bbl_ok).sum(), "dropped (no building ID)")
    df = df[bbl_ok]
    df["bbl"] = df["bbl"].astype(str).str.replace(r"\.0$", "", regex=True)

    # 6. Coordinates missing or outside NYC - keep (BBL is the key) but flag.
    lat = pd.to_numeric(df["latitude"], errors="coerce")
    lon = pd.to_numeric(df["longitude"], errors="coerce")
    geo_bad = ~(lat.between(*NYC_LAT) & lon.between(*NYC_LON))
    note("coords_missing_or_outside_nyc", geo_bad.sum(), "flagged (geo_flag = 1)")
    df["latitude"], df["longitude"] = lat, lon
    df["geo_flag"] = geo_bad.astype(int)

    # 7. Normalize borough labels.
    df["borough"] = df["borough"].str.upper().str.strip().replace({"": pd.NA, "UNSPECIFIED": pd.NA})

    note("rows_loaded", len(df), "none")
    return df.reset_index(drop=True), pd.DataFrame(report)


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def load(df: pd.DataFrame, report: pd.DataFrame, db_path: str | Path) -> None:
    """Create the schema and write clean records plus the quality log."""
    out = df.copy()
    for col in ("created_date", "closed_date"):
        out[col] = out[col].dt.strftime("%Y-%m-%dT%H:%M:%S")

    report = report.assign(run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    with sqlite3.connect(db_path) as con:
        con.executescript((SQL_DIR / "schema.sql").read_text())
        out.to_sql("complaints", con, if_exists="append", index=False)
        report[["run_at", "check_name", "n_rows", "action"]].to_sql(
            "data_quality_log", con, if_exists="append", index=False)
    log.info("Loaded %s rows into %s", f"{len(out):,}", db_path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", required=True, help="inclusive, YYYY-MM-DD")
    p.add_argument("--end", required=True, help="exclusive, YYYY-MM-DD")
    p.add_argument("--db", default="heat.db")
    p.add_argument("--app-token", default=None, help="optional Socrata app token (higher rate limits)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raw = extract(args.start, args.end, args.app_token)
    clean, report = validate(raw, args.start, args.end)
    load(clean, report, args.db)


if __name__ == "__main__":
    main()
