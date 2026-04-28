"""
EIA-860 Plant Data Pull for PJM — FIXED
========================================
Handles EIA's redirect/user-agent requirements that cause
"BadZipFile: File is not a zip file" errors.

Fix: Uses browser-like headers and validates the download
before attempting to unzip. Falls back to manual download
instructions if automated download fails.
"""

import pandas as pd
import os
import zipfile
import io
import requests
from pathlib import Path

# ── Configuration ──────────────────────────────────────────
YEAR = 2023
OUTPUT_DIR = Path("data/raw")
PROCESSED_DIR = Path("data/processed")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

PJM_STATES = [
    "PA", "NJ", "MD", "DE", "VA", "WV", "OH", "IN", "IL",
    "MI", "KY", "NC", "TN", "DC"
]

# EIA changes URL patterns — try multiple known formats
ZIP_URLS = [
    f"https://www.eia.gov/electricity/data/eia860/xls/eia860{YEAR}.zip",
    f"https://www.eia.gov/electricity/data/eia860/archive/xls/eia860{YEAR}.zip",
]

# Browser-like headers to prevent EIA from serving an HTML page
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/zip, application/octet-stream, */*",
}


# ── Step 1: Download the ZIP ──────────────────────────────
def download_eia860():
    zip_path = OUTPUT_DIR / f"eia860{YEAR}.zip"

    # If already downloaded and valid, skip
    if zip_path.exists():
        if zipfile.is_zipfile(zip_path):
            print(f"[skip] {zip_path.name} already exists and is valid")
            return zip_path
        else:
            print(f"[delete] {zip_path.name} is NOT a valid ZIP — deleting and re-downloading")
            zip_path.unlink()

    # Try each URL
    for url in ZIP_URLS:
        print(f"Trying: {url}")
        try:
            resp = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=60)

            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                print(f"  Got HTML, not ZIP. Trying next URL...")
                continue

            if resp.status_code != 200:
                print(f"  HTTP {resp.status_code}. Trying next URL...")
                continue

            # Validate ZIP magic bytes (PK\x03\x04)
            if resp.content[:4] != b'PK\x03\x04':
                print(f"  Not a ZIP file (wrong header bytes). Trying next URL...")
                continue

            with open(zip_path, "wb") as f:
                f.write(resp.content)

            size_mb = len(resp.content) / (1024 * 1024)
            print(f"  Success: {size_mb:.1f} MB")
            return zip_path

        except requests.RequestException as e:
            print(f"  Error: {e}. Trying next URL...")
            continue

    # All URLs failed — manual fallback
    print()
    print("=" * 60)
    print("AUTOMATED DOWNLOAD FAILED — MANUAL STEPS BELOW")
    print("=" * 60)
    print()
    print("EIA's server is blocking the automated download.")
    print("This is a known issue — their site requires a real browser.")
    print()
    print("To fix this:")
    print()
    print("  1. Open your browser and go to:")
    print("     https://www.eia.gov/electricity/data/eia860/")
    print()
    print(f"  2. Scroll down and click the {YEAR} ZIP download link")
    print()
    print(f"  3. Save the file as:")
    print(f"     {zip_path.absolute()}")
    print()
    print("  4. Re-run this script — it will detect the file and process it.")
    print()

    if zip_path.exists() and zipfile.is_zipfile(zip_path):
        print("Found manually downloaded file! Continuing...")
        return zip_path

    return None


# ── Step 2: Read plant data ───────────────────────────────
def read_plant_data(zip_path):
    print(f"\nReading plant data...")

    with zipfile.ZipFile(zip_path, "r") as z:
        all_files = z.namelist()
        print(f"  Files in ZIP ({len(all_files)}):")
        for f in sorted(all_files):
            if "__MACOSX" not in f:
                print(f"    {f}")

        plant_files = [f for f in all_files
                       if "plant" in f.lower()
                       and f.endswith((".xlsx", ".xls"))
                       and "~" not in f and "__MACOSX" not in f]

        if not plant_files:
            print("  ERROR: No plant file found")
            return None

        plant_file = plant_files[0]
        print(f"\n  Parsing: {plant_file}")

        with z.open(plant_file) as f:
            df = pd.read_excel(io.BytesIO(f.read()), sheet_name=0, header=1, dtype=str)

    print(f"  Rows: {len(df):,}  Cols: {len(df.columns)}")
    return df


# ── Step 3: Read generator data ───────────────────────────
def read_generator_data(zip_path):
    print(f"\nReading generator data...")

    with zipfile.ZipFile(zip_path, "r") as z:
        all_files = z.namelist()
        gen_files = [f for f in all_files
                     if "generator" in f.lower() and "3_1" in f
                     and f.endswith((".xlsx", ".xls"))
                     and "~" not in f and "__MACOSX" not in f]

        if not gen_files:
            gen_files = [f for f in all_files
                         if "generator" in f.lower()
                         and f.endswith((".xlsx", ".xls"))
                         and "~" not in f and "__MACOSX" not in f
                         and "wind" not in f.lower() and "solar" not in f.lower()
                         and "storage" not in f.lower()]

        if not gen_files:
            print("  No generator file found — skipping")
            return None

        gen_file = gen_files[0]
        print(f"  Parsing: {gen_file}")

        with z.open(gen_file) as f:
            data = io.BytesIO(f.read())
            try:
                df = pd.read_excel(data, sheet_name="Operable", header=1, dtype=str)
            except (ValueError, KeyError):
                data.seek(0)
                df = pd.read_excel(data, sheet_name=0, header=1, dtype=str)

    print(f"  Rows: {len(df):,}")
    return df


# ── Step 4: Filter and process ────────────────────────────
def process_pjm_plants(plant_df, gen_df):
    print(f"\nFiltering for PJM states: {', '.join(PJM_STATES)}")

    def find_col(df, keywords, exclude=None):
        for c in df.columns:
            cl = c.lower().strip()
            if all(k in cl for k in keywords):
                if exclude and any(e in cl for e in exclude):
                    continue
                return c
        return None

    state_col = find_col(plant_df, ["state"])
    if not state_col:
        print(f"  ERROR: No state column. Columns: {list(plant_df.columns)}")
        return None

    plants = plant_df[plant_df[state_col].str.strip().str.upper().isin(PJM_STATES)].copy()
    print(f"  PJM plants: {len(plants):,}")

    plant_code_col = find_col(plant_df, ["plant", "code"]) or find_col(plant_df, ["plant id"])
    plant_name_col = find_col(plant_df, ["plant", "name"])
    lat_col = find_col(plant_df, ["latitude"])
    lon_col = find_col(plant_df, ["longitude"])
    ba_col = find_col(plant_df, ["balancing"])
    county_col = find_col(plant_df, ["county"])

    clean = pd.DataFrame()
    if plant_code_col: clean["plant_code"] = plants[plant_code_col].str.strip()
    if plant_name_col: clean["plant_name"] = plants[plant_name_col].str.strip()
    clean["state"] = plants[state_col].str.strip().str.upper()
    if county_col: clean["county"] = plants[county_col].str.strip()
    if lat_col: clean["latitude"] = pd.to_numeric(plants[lat_col], errors="coerce")
    if lon_col: clean["longitude"] = pd.to_numeric(plants[lon_col], errors="coerce")
    if ba_col: clean["balancing_authority"] = plants[ba_col].str.strip()

    # Merge generator-level aggregations
    if gen_df is not None and "plant_code" in clean.columns:
        gpc = find_col(gen_df, ["plant", "code"]) or find_col(gen_df, ["plant id"])
        gcc = find_col(gen_df, ["nameplate", "capacity"]) or find_col(gen_df, ["nameplate"])
        gfc = find_col(gen_df, ["energy", "source"]) or find_col(gen_df, ["fuel"])

        if gpc and gcc:
            gen_df["_cap"] = pd.to_numeric(gen_df[gcc], errors="coerce")
            gen_df["_pc"] = gen_df[gpc].str.strip()

            agg = gen_df.groupby("_pc").agg(
                total_capacity_mw=("_cap", "sum"),
                generator_count=("_cap", "count")
            ).reset_index().rename(columns={"_pc": "plant_code"})

            if gfc:
                idx = gen_df.groupby("_pc")["_cap"].idxmax()
                fuel = gen_df.loc[idx, ["_pc", gfc]].rename(
                    columns={"_pc": "plant_code", gfc: "primary_fuel"})
                agg = agg.merge(fuel, on="plant_code", how="left")

            clean = clean.merge(agg, on="plant_code", how="left")

    outpath = PROCESSED_DIR / "pjm_plants.csv"
    clean.to_csv(outpath, index=False)

    print(f"\nSaved: {outpath}")
    print(f"Plants: {len(clean):,}")
    if "total_capacity_mw" in clean.columns:
        print(f"Total capacity: {clean['total_capacity_mw'].sum():,.0f} MW")
        if "primary_fuel" in clean.columns:
            print(f"\nBy fuel type:")
            for fuel, grp in sorted(clean.groupby("primary_fuel"),
                                     key=lambda x: -x[1]["total_capacity_mw"].sum()):
                c = grp["total_capacity_mw"].sum()
                if c > 100:
                    print(f"  {fuel:6s}  {len(grp):>4} plants  {c:>10,.0f} MW")

    return clean


# ── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("EIA-860 Plant Data Pipeline for PJM (FIXED)")
    print("=" * 55)

    zip_path = download_eia860()
    if zip_path is None:
        exit(1)

    plant_df = read_plant_data(zip_path)
    gen_df = read_generator_data(zip_path)

    if plant_df is not None:
        process_pjm_plants(plant_df, gen_df)

    print("\nDone.")