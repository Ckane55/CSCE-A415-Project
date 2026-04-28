"""
Open-Meteo Weather Data Pull for PJM
======================================
Pulls hourly historical weather data (ERA5 reanalysis) for
each PJM load zone centroid using the free Open-Meteo API.

No API key required. No rate limits for research use.
Data source: ERA5 reanalysis via https://open-meteo.com/

Variables pulled:
  - temperature_2m (°F, converted from °C)
  - relative_humidity_2m (%)
  - wind_speed_10m (mph, converted from km/h)
  - cloud_cover (%)
  - precipitation (inches, converted from mm)

Computed features:
  - HDD (heating degree days): max(0, 65 - temp_f)
  - CDD (cooling degree days): max(0, temp_f - 65)
"""

import requests
import pandas as pd
import time
from pathlib import Path

# ── Configuration ──────────────────────────────────────────
OUTPUT_DIR = Path("data/processed")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "2015-07-01"  # Match EIA-930 start date
END_DATE = "2024-12-31"

# PJM zone centroids (approximate city centers for each major zone)
# These are the representative weather locations for each PJM subregion
PJM_ZONES = {
    "PECO":    {"lat": 39.95, "lon": -75.17, "city": "Philadelphia, PA"},
    "PSEG":    {"lat": 40.74, "lon": -74.17, "city": "Newark, NJ"},
    "BGE":     {"lat": 39.29, "lon": -76.61, "city": "Baltimore, MD"},
    "PEPCO":   {"lat": 38.91, "lon": -77.04, "city": "Washington, DC"},
    "DOM":     {"lat": 37.54, "lon": -77.43, "city": "Richmond, VA"},
    "AEP":     {"lat": 39.96, "lon": -82.99, "city": "Columbus, OH"},
    "COMED":   {"lat": 41.88, "lon": -87.63, "city": "Chicago, IL"},
    "DUQ":     {"lat": 40.44, "lon": -80.00, "city": "Pittsburgh, PA"},
    "PPL":     {"lat": 40.60, "lon": -75.47, "city": "Allentown, PA"},
    "JCPL":    {"lat": 40.22, "lon": -74.01, "city": "Lakewood, NJ"},
    "METED":   {"lat": 40.34, "lon": -76.41, "city": "Reading, PA"},
    "DPL":     {"lat": 39.16, "lon": -75.52, "city": "Dover, DE"},
    "ATSI":    {"lat": 41.10, "lon": -80.65, "city": "Youngstown, OH"},
    "DAYTON":  {"lat": 39.76, "lon": -84.19, "city": "Dayton, OH"},
    "DEOK":    {"lat": 39.10, "lon": -84.51, "city": "Cincinnati, OH"},
}

# Open-Meteo API base URL for historical data
API_URL = "https://archive-api.open-meteo.com/v1/archive"

# Weather variables to pull
HOURLY_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "cloud_cover",
    "precipitation",
]


# ── Pull weather for one zone ─────────────────────────────
def pull_zone_weather(zone_name, lat, lon, start_date, end_date):
    """
    Pull hourly weather data for a single zone from Open-Meteo.
    Returns a DataFrame with datetime index and weather columns.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": ",".join(HOURLY_VARS),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "UTC",
    }

    # Retry up to 5 times on rate limit (429) errors
    max_retries = 5
    for attempt in range(max_retries):
        resp = requests.get(API_URL, params=params, timeout=120)

        if resp.status_code == 429:
            wait = 65 * (attempt + 1)  # 65s, 130s, 195s...
            print(f"    Rate limited (429). Waiting {wait}s before retry {attempt + 2}/{max_retries}...")
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            print(f"    ERROR {resp.status_code}: {resp.text[:200]}")
            return None

        break
    else:
        print(f"    FAILED after {max_retries} retries — rate limit not clearing")
        return None

    data = resp.json()

    if "error" in data and data["error"]:
        print(f"    API ERROR: {data.get('reason', 'unknown')}")
        return None

    hourly = data.get("hourly", {})
    if not hourly or "time" not in hourly:
        print(f"    ERROR: No hourly data returned")
        return None

    df = pd.DataFrame(hourly)
    df["datetime_utc"] = pd.to_datetime(df["time"])
    df = df.drop(columns=["time"])
    df["zone"] = zone_name

    # Rename columns for clarity
    df = df.rename(columns={
        "temperature_2m": "temp_f",
        "relative_humidity_2m": "humidity_pct",
        "wind_speed_10m": "wind_mph",
        "cloud_cover": "cloud_pct",
        "precipitation": "precip_in",
    })

    # ── Compute HDD and CDD ──────────────────────────────
    # HDD = max(0, 65 - temperature) — measures heating demand
    # CDD = max(0, temperature - 65) — measures cooling demand
    # 65°F is the standard balance point used in utility forecasting
    df["hdd"] = (65 - df["temp_f"]).clip(lower=0)
    df["cdd"] = (df["temp_f"] - 65).clip(lower=0)

    return df


# ── Pull all zones ────────────────────────────────────────
def pull_all_zones():
    """Pull weather for all PJM zones and save combined + individual files."""
    all_frames = []

    print(f"Pulling weather data for {len(PJM_ZONES)} PJM zones")
    print(f"Date range: {START_DATE} to {END_DATE}")
    print(f"Source: Open-Meteo ERA5 reanalysis")
    print()

    for i, (zone, info) in enumerate(PJM_ZONES.items(), 1):
        # Skip zones already downloaded (lets you resume after interruption)
        zone_path = OUTPUT_DIR / f"weather_{zone.lower()}.csv"
        if zone_path.exists():
            print(f"[{i}/{len(PJM_ZONES)}] {zone} — already downloaded, loading from disk")
            df = pd.read_csv(zone_path)
            df["datetime_utc"] = pd.to_datetime(df["datetime_utc"])
            all_frames.append(df)
            continue

        print(f"[{i}/{len(PJM_ZONES)}] {zone} — {info['city']} "
              f"({info['lat']}, {info['lon']})")

        df = pull_zone_weather(zone, info["lat"], info["lon"], START_DATE, END_DATE)

        if df is not None:
            print(f"    {len(df):,} hours  "
                  f"temp range: {df['temp_f'].min():.0f}°F to {df['temp_f'].max():.0f}°F  "
                  f"mean: {df['temp_f'].mean():.1f}°F")
            all_frames.append(df)

            # Save individual zone file
            zone_path = OUTPUT_DIR / f"weather_{zone.lower()}.csv"
            df.to_csv(zone_path, index=False)
        else:
            print(f"    FAILED — skipping")

        # Wait 65 seconds between zones to stay under the per-minute rate limit.
        # Each request pulls ~10 years of hourly data, which is heavy.
        # Total runtime: ~15 zones × 65s = ~16 minutes. Go grab coffee.
        if i < len(PJM_ZONES):
            print(f"    Waiting 65s before next zone (rate limit cooldown)...")
            time.sleep(65)

    if not all_frames:
        print("\nERROR: No weather data pulled for any zone.")
        return None

    # Combine all zones
    combined = pd.concat(all_frames, ignore_index=True)
    combined = combined.sort_values(["zone", "datetime_utc"]).reset_index(drop=True)

    # Save combined file
    combined_path = OUTPUT_DIR / "pjm_weather_all_zones.csv"
    combined.to_csv(combined_path, index=False)

    print(f"\n{'=' * 55}")
    print(f"COMPLETE")
    print(f"{'=' * 55}")
    print(f"Total rows:  {len(combined):,}")
    print(f"Zones:       {combined['zone'].nunique()}")
    print(f"Date range:  {combined['datetime_utc'].min()} to {combined['datetime_utc'].max()}")
    print(f"\nSaved:")
    print(f"  Combined:   {combined_path}")
    print(f"  Individual: {OUTPUT_DIR}/weather_<zone>.csv")
    print(f"\nColumns: datetime_utc, zone, temp_f, humidity_pct, wind_mph,")
    print(f"         cloud_pct, precip_in, hdd, cdd")

    # Summary stats
    print(f"\nZone summary:")
    for zone, grp in combined.groupby("zone"):
        print(f"  {zone:8s}  "
              f"mean={grp['temp_f'].mean():5.1f}°F  "
              f"min={grp['temp_f'].min():6.1f}°F  "
              f"max={grp['temp_f'].max():5.1f}°F  "
              f"avg HDD={grp['hdd'].mean():4.1f}  "
              f"avg CDD={grp['cdd'].mean():4.1f}")

    return combined


# ── Quick single-zone test ────────────────────────────────
def test_single_zone():
    """
    Quick test: pull one month of data for Philadelphia
    to verify the API is working before pulling the full dataset.
    """
    print("Quick test: pulling 1 month for Philadelphia...")
    df = pull_zone_weather("PECO", 39.95, -75.17, "2024-01-01", "2024-01-31")

    if df is not None:
        print(f"  Rows: {len(df)}")
        print(f"  Temp range: {df['temp_f'].min():.0f}°F to {df['temp_f'].max():.0f}°F")
        print(f"  HDD range: {df['hdd'].min():.1f} to {df['hdd'].max():.1f}")
        print(f"  CDD range: {df['cdd'].min():.1f} to {df['cdd'].max():.1f}")
        print(f"\n  First 5 rows:")
        print(df.head().to_string())
        print(f"\n  API is working. Run pull_all_zones() for the full dataset.")
        return True
    else:
        print("  FAILED. Check your internet connection.")
        return False


# ── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("Open-Meteo Weather Pull for PJM")
    print("=" * 55)
    print()

    # Step 1: Quick test
    if not test_single_zone():
        exit(1)

    print()
    print("-" * 55)
    print()

    # Step 2: Pull all zones
    combined = pull_all_zones()

    if combined is not None:
        print(f"\nDone. Next step: merge weather with EIA-930 demand data.")