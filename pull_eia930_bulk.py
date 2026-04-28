"""
EIA-930 Data Pull for PJM — Bulk CSV Method
=============================================
Downloads 6-month CSV files from EIA's Hourly Grid Monitor,
filters for PJM, and saves a clean hourly demand dataset.

No API key required. Just run this script.

Data source: https://www.eia.gov/electricity/gridmonitor/
File naming: EIA930_BALANCE_{YYYY}_{period}.csv
  where period = "Jan_Jun" or "Jul_Dec"
"""

import pandas as pd
import os
from pathlib import Path

# ── Configuration ──────────────────────────────────────────
OUTPUT_DIR = Path("data/raw")
PROCESSED_DIR = Path("data/processed")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# EIA-930 bulk CSV base URL
BASE_URL = "https://www.eia.gov/electricity/gridmonitor/sixMonthFiles"

# Generate file list: Jul 2015 through latest available
# Files are named: EIA930_BALANCE_2015_Jul_Dec.csv, EIA930_BALANCE_2016_Jan_Jun.csv, etc.
def get_file_urls(start_year=2015, end_year=2024):
    urls = []
    for year in range(start_year, end_year + 1):
        if year == 2015:
            # Data starts Jul 2015
            urls.append(f"{BASE_URL}/EIA930_BALANCE_{year}_Jul_Dec.csv")
        else:
            urls.append(f"{BASE_URL}/EIA930_BALANCE_{year}_Jan_Jun.csv")
            urls.append(f"{BASE_URL}/EIA930_BALANCE_{year}_Jul_Dec.csv")
    return urls


# ── Step 1: Download CSVs ─────────────────────────────────
def download_csvs():
    """Download all 6-month CSV files from EIA."""
    urls = get_file_urls()
    print(f"Downloading {len(urls)} files...")

    for url in urls:
        filename = url.split("/")[-1]
        filepath = OUTPUT_DIR / filename

        if filepath.exists():
            print(f"  [skip] {filename} already exists")
            continue

        print(f"  [download] {filename}")
        try:
            df = pd.read_csv(url, low_memory=False)
            df.to_csv(filepath, index=False)
            print(f"    → {len(df):,} rows")
        except Exception as e:
            print(f"    → ERROR: {e}")

    print("Download complete.\n")


# ── Step 2: Filter for PJM and combine ────────────────────
def process_pjm():
    """
    Read all downloaded CSVs, filter for PJM balancing authority,
    parse timestamps, and save a single clean file.
    """
    csv_files = sorted(OUTPUT_DIR.glob("EIA930_BALANCE_*.csv"))
    if not csv_files:
        print("ERROR: No CSV files found. Run download_csvs() first.")
        return None

    print(f"Processing {len(csv_files)} files for PJM...")
    frames = []

    for f in csv_files:
        print(f"  Reading {f.name}...")
        df = pd.read_csv(f, low_memory=False)

        # The balancing authority column is named "Balancing Authority"
        # PJM's BA code is "PJM"
        ba_col = [c for c in df.columns if "balancing" in c.lower() and "authority" in c.lower()]
        if not ba_col:
            # Try alternate column name
            ba_col = [c for c in df.columns if c.strip().upper() == "BA_CODE" or "respondent" in c.lower()]

        if ba_col:
            col = ba_col[0]
            pjm = df[df[col].astype(str).str.strip().str.upper() == "PJM"].copy()
        else:
            print(f"    → WARNING: Could not find BA column in {f.name}")
            print(f"      Columns: {list(df.columns[:10])}")
            continue

        print(f"    → {len(pjm):,} PJM rows out of {len(df):,} total")
        frames.append(pjm)

    if not frames:
        print("ERROR: No PJM data found in any file.")
        return None

    # Combine all periods
    combined = pd.concat(frames, ignore_index=True)
    print(f"\nCombined: {len(combined):,} total PJM rows")

    # ── Parse timestamps ──────────────────────────────────
    # EIA-930 CSVs have columns like:
    #   "UTC Time at End of Hour" or "Data Date" + "Hour Number"
    # The exact column names vary by file vintage, so we search.

    time_cols = [c for c in combined.columns if "utc" in c.lower() and "time" in c.lower()]
    if time_cols:
        combined["datetime_utc"] = pd.to_datetime(combined[time_cols[0]], errors="coerce")
    else:
        # Fallback: look for "Data Date" + "Hour Number"
        date_cols = [c for c in combined.columns if "date" in c.lower()]
        hour_cols = [c for c in combined.columns if "hour" in c.lower() and "number" in c.lower()]
        if date_cols and hour_cols:
            combined["datetime_utc"] = pd.to_datetime(
                combined[date_cols[0]].astype(str) + " " +
                combined[hour_cols[0]].astype(str).str.zfill(2) + ":00",
                errors="coerce"
            )
        else:
            # Last resort: use first column that looks like a timestamp
            for c in combined.columns:
                try:
                    combined["datetime_utc"] = pd.to_datetime(combined[c], errors="coerce")
                    if combined["datetime_utc"].notna().sum() > len(combined) * 0.5:
                        break
                except:
                    continue

    # ── Identify demand column ────────────────────────────
    demand_cols = [c for c in combined.columns if "demand" in c.lower() and "forecast" not in c.lower()]
    if demand_cols:
        # Take the first one that looks like total demand (not subregion)
        demand_col = demand_cols[0]
        combined["demand_mw"] = pd.to_numeric(combined[demand_col], errors="coerce")
    else:
        print("WARNING: Could not identify demand column automatically.")
        print(f"Available columns: {list(combined.columns)}")

    # ── Clean and sort ────────────────────────────────────
    combined = combined.dropna(subset=["datetime_utc"])
    combined = combined.sort_values("datetime_utc").reset_index(drop=True)
    combined = combined.drop_duplicates(subset=["datetime_utc"], keep="last")

    # ── Save ──────────────────────────────────────────────
    outpath = PROCESSED_DIR / "pjm_hourly_demand.csv"
    # Save key columns
    cols_to_keep = ["datetime_utc"]
    if "demand_mw" in combined.columns:
        cols_to_keep.append("demand_mw")
    # Keep any subregion demand columns too
    for c in combined.columns:
        if "demand" in c.lower() and c not in cols_to_keep:
            cols_to_keep.append(c)

    output = combined[cols_to_keep].copy()
    output.to_csv(outpath, index=False)

    print(f"\nSaved: {outpath}")
    print(f"Date range: {output['datetime_utc'].min()} to {output['datetime_utc'].max()}")
    print(f"Total hours: {len(output):,}")
    print(f"Missing hours: {output['demand_mw'].isna().sum() if 'demand_mw' in output.columns else 'N/A'}")

    return output


# ── Step 3: Quick sanity check ────────────────────────────
def sanity_check(df):
    """Print basic stats to verify the data looks right."""
    if df is None:
        return

    print("\n" + "=" * 50)
    print("SANITY CHECK")
    print("=" * 50)

    if "demand_mw" in df.columns:
        print(f"Mean demand:  {df['demand_mw'].mean():,.0f} MW")
        print(f"Min demand:   {df['demand_mw'].min():,.0f} MW")
        print(f"Max demand:   {df['demand_mw'].max():,.0f} MW")
        print(f"Std demand:   {df['demand_mw'].std():,.0f} MW")

        # Check for suspicious values
        zeros = (df["demand_mw"] == 0).sum()
        negatives = (df["demand_mw"] < 0).sum()
        if zeros > 0:
            print(f"\n⚠  {zeros} zero-demand hours (likely reporting errors)")
        if negatives > 0:
            print(f"⚠  {negatives} negative-demand hours (definitely errors)")

    # Check for gaps
    if "datetime_utc" in df.columns:
        df_sorted = df.sort_values("datetime_utc")
        diffs = df_sorted["datetime_utc"].diff()
        gaps = diffs[diffs > pd.Timedelta(hours=1)]
        if len(gaps) > 0:
            print(f"\n⚠  {len(gaps)} gaps in hourly continuity:")
            for idx in gaps.head(5).index:
                print(f"   Gap at {df_sorted.loc[idx, 'datetime_utc']} "
                      f"({diffs.loc[idx]})")

    print("\nFirst 5 rows:")
    print(df.head().to_string())


# ── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("EIA-930 PJM Data Pipeline")
    print("=" * 50)

    # Step 1: Download
    download_csvs()

    # Step 2: Process
    df = process_pjm()

    # Step 3: Verify
    sanity_check(df)

    print("\n✓ Done. Your PJM hourly demand data is in data/processed/pjm_hourly_demand.csv")
    print("  Next step: pull weather data from Open-Meteo for PJM zone centroids.")