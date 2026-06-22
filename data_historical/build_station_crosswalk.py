import argparse
import io
import json
import os
import time
import urllib.request
import zipfile
from multiprocessing import Pool

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from sklearn.neighbors import BallTree

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'data_ingestion', '.env'))

ZIP_2019 = r"C:\Users\clark\Desktop\citibike_csv_zipfiles\2019-citibike-tripdata.zip"

# Five snapshots spanning 2017–2021 station additions.
# URL form uses id_ flag to get the raw original JSON (not Wayback-injected HTML).
# Station counts from CLAUDE.md: 668 (2017) → 1,587 (2021-12).
WAYBACK_SNAPSHOTS = [
    "20170701120000",
    "20181201120000",
    "20201001120000",
    "20210501120000",
    "20211201120000",
]
GBFS_STATION_INFO_URL = "https://gbfs.citibikenyc.com/gbfs/en/station_information.json"

EARTH_RADIUS_M    = 6_371_000
RADIUS_400M       = 400 / EARTH_RADIUS_M
RADIUS_800M       = 800 / EARTH_RADIUS_M
MATCH_THRESHOLD_M = 100


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


# ---------------------------------------------------------------------------
# Step 1 — extract legacy station coords from 2019 trip CSVs (parallel)
# ---------------------------------------------------------------------------

def _parse_csv_bytes(csv_bytes: bytes) -> pd.DataFrame:
    """Parse one CSV's bytes and return station_id, lat, lon rows."""
    df = pd.read_csv(
        io.BytesIO(csv_bytes),
        usecols=["start station id", "start station latitude",
                 "start station longitude",
                 "end station id", "end station latitude",
                 "end station longitude"],
        dtype=str,
        encoding="latin-1",
    )
    parts = []
    for id_col, lat_col, lon_col in [
        ("start station id", "start station latitude", "start station longitude"),
        ("end station id",   "end station latitude",   "end station longitude"),
    ]:
        sub = df[[id_col, lat_col, lon_col]].copy()
        sub.columns = ["station_id", "lat", "lon"]
        parts.append(sub)
    return pd.concat(parts, ignore_index=True)


def extract_legacy_coords(zip_path: str, workers: int) -> pd.DataFrame:
    """Return DataFrame with columns: station_id (str), lat, lon."""
    z = zipfile.ZipFile(zip_path)
    csvs = [f for f in z.namelist() if f.endswith(".csv") and not os.path.basename(f).startswith("._")]
    print(f"  Found {len(csvs)} CSVs in 2019 zip — reading into memory...")

    # read all CSV bytes sequentially (zipfile is not process-safe for concurrent reads)
    all_bytes = [z.read(name) for name in csvs]

    print(f"  Parsing CSVs with {workers} workers...")
    with Pool(workers) as pool:
        frames = pool.map(_parse_csv_bytes, all_bytes)

    all_rows = pd.concat(frames, ignore_index=True)
    all_rows["lat"] = pd.to_numeric(all_rows["lat"], errors="coerce")
    all_rows["lon"] = pd.to_numeric(all_rows["lon"], errors="coerce")
    all_rows = all_rows.dropna(subset=["lat", "lon"])
    all_rows["station_id"] = all_rows["station_id"].str.strip()
    all_rows = all_rows.dropna(subset=["station_id"])
    all_rows = all_rows[all_rows["station_id"].str.match(r"^\d+$", na=False)]

    coords = (all_rows.groupby("station_id")[["lat", "lon"]]
                      .mean()
                      .reset_index())
    print(f"  {len(coords)} unique legacy station IDs extracted")
    return coords


# ---------------------------------------------------------------------------
# Step 1b — fetch legacy station coords from Wayback Machine GBFS snapshots
# The old feed (pre-UUID era) used integer station_id as the key, with lat/lon.
# We union 5 snapshots spanning 2017–2021 to cover stations added after 2019.
# ---------------------------------------------------------------------------

def _fetch_wayback_snapshot(timestamp: str) -> pd.DataFrame:
    """Fetch one Wayback Machine GBFS snapshot and return station_id/lat/lon rows."""
    url = (
        f"http://web.archive.org/web/{timestamp}id_/{GBFS_STATION_INFO_URL}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "citibike-research/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        stations = payload.get("data", {}).get("stations", [])
        rows = []
        for s in stations:
            sid = str(s.get("station_id", "")).strip()
            lat = s.get("lat")
            lon = s.get("lon")
            if sid and lat is not None and lon is not None:
                rows.append({"station_id": sid, "lat": float(lat), "lon": float(lon)})
        df = pd.DataFrame(rows)
        print(f"    {timestamp}: {len(df)} stations")
        return df
    except Exception as exc:
        print(f"    {timestamp}: FAILED ({exc})")
        return pd.DataFrame(columns=["station_id", "lat", "lon"])


def fetch_wayback_coords() -> pd.DataFrame:
    """
    Fetch 5 Wayback GBFS snapshots, union them, and return unique integer-ID
    station coords not already captured by the 2019 trip CSVs.
    Earlier snapshots take precedence (most stable coords for a given station).
    """
    frames = []
    for ts in WAYBACK_SNAPSHOTS:
        df = _fetch_wayback_snapshot(ts)
        frames.append(df)
        time.sleep(1)  # be polite to archive.org

    if not frames:
        return pd.DataFrame(columns=["station_id", "lat", "lon"])

    combined = pd.concat(frames, ignore_index=True)
    combined["lat"] = pd.to_numeric(combined["lat"], errors="coerce")
    combined["lon"] = pd.to_numeric(combined["lon"], errors="coerce")
    combined = combined.dropna(subset=["lat", "lon"])
    # keep only legacy integer IDs (exclude JC119, HB101, etc.)
    combined = combined[combined["station_id"].str.match(r"^\d+$", na=False)]
    # earliest snapshot wins for each station_id — concat is ordered, so first() is correct
    coords = combined.groupby("station_id")[["lat", "lon"]].first().reset_index()
    print(f"  {len(coords)} unique legacy station IDs from Wayback snapshots")
    return coords


# ---------------------------------------------------------------------------
# Step 2 — create crosswalk table and BallTree-match to modern station_information
# (already vectorized numpy — no multiprocessing benefit on ~2k stations)
# ---------------------------------------------------------------------------

DDL_CROSSWALK = """
CREATE TABLE IF NOT EXISTS station_id_crosswalk (
    legacy_id       VARCHAR(50)      PRIMARY KEY,
    modern_uuid     VARCHAR(255),
    lat             DOUBLE PRECISION,
    lon             DOUBLE PRECISION,
    match_dist_m    DOUBLE PRECISION
);
"""


def build_crosswalk(cur, legacy_coords: pd.DataFrame):
    cur.execute(DDL_CROSSWALK)

    cur.execute(
        "SELECT station_id, lat, lon FROM station_information "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL"
    )
    modern_rows       = cur.fetchall()
    modern_ids        = [r[0] for r in modern_rows]
    modern_coords_rad = np.radians([[float(r[1]), float(r[2])] for r in modern_rows])
    legacy_coords_rad = np.radians(legacy_coords[["lat", "lon"]].values)

    tree = BallTree(modern_coords_rad, metric="haversine")
    dist_rad, idx = tree.query(legacy_coords_rad, k=1)
    dist_m = dist_rad[:, 0] * EARTH_RADIUS_M

    flagged = (dist_m > MATCH_THRESHOLD_M).sum()
    if flagged:
        print(f"  WARNING: {flagged} legacy stations matched >100m from nearest modern station")

    crosswalk = legacy_coords.copy()
    crosswalk["modern_uuid"]  = [modern_ids[i] for i in idx[:, 0]]
    crosswalk["match_dist_m"] = dist_m

    records = [
        (row.station_id, row.modern_uuid, row.lat, row.lon, row.match_dist_m)
        for row in crosswalk.itertuples()
    ]
    cur.executemany(
        """
        INSERT INTO station_id_crosswalk (legacy_id, modern_uuid, lat, lon, match_dist_m)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (legacy_id) DO UPDATE SET
            modern_uuid  = EXCLUDED.modern_uuid,
            lat          = EXCLUDED.lat,
            lon          = EXCLUDED.lon,
            match_dist_m = EXCLUDED.match_dist_m
        """,
        records,
    )
    print(f"  {len(records)} rows upserted into station_id_crosswalk")


# ---------------------------------------------------------------------------
# Step 3 — compute MTA proximity for legacy station coords
# (already vectorized numpy — no multiprocessing benefit on ~2k stations)
# ---------------------------------------------------------------------------

def compute_proximity(cur, legacy_coords: pd.DataFrame):
    cur.execute("SELECT entrance_latitude, entrance_longitude FROM mta_subway_entrances")
    mta_rows = cur.fetchall()
    if not mta_rows:
        raise RuntimeError("mta_subway_entrances is empty — run fetch_mta_entrances.py first")
    mta_coords_rad    = np.radians([[float(r[0]), float(r[1])] for r in mta_rows])
    legacy_coords_rad = np.radians(legacy_coords[["lat", "lon"]].values)

    tree             = BallTree(mta_coords_rad, metric="haversine")
    dist_nearest, _  = tree.query(legacy_coords_rad, k=1)
    counts_400       = tree.query_radius(legacy_coords_rad, r=RADIUS_400M, count_only=True)
    counts_800       = tree.query_radius(legacy_coords_rad, r=RADIUS_800M, count_only=True)

    records = []
    for i, station_id in enumerate(legacy_coords["station_id"]):
        dist_m  = float(dist_nearest[i][0]) * EARTH_RADIUS_M
        c400    = int(counts_400[i])
        c800    = int(counts_800[i])
        nearest = float(dist_m) if c800 > 0 else None
        records.append((str(station_id), nearest, c400, c800, bool(c400 > 0)))

    cur.executemany(
        """
        INSERT INTO citibike_station_subway_proximity (
            citibike_station_id, nearest_entrance_dist_m,
            entrance_count_400m, entrance_count_800m, is_within_400m
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (citibike_station_id) DO UPDATE SET
            nearest_entrance_dist_m = EXCLUDED.nearest_entrance_dist_m,
            entrance_count_400m     = EXCLUDED.entrance_count_400m,
            entrance_count_800m     = EXCLUDED.entrance_count_800m,
            is_within_400m          = EXCLUDED.is_within_400m
        """,
        records,
    )
    print(f"  {len(records)} legacy stations upserted into citibike_station_subway_proximity")


# ---------------------------------------------------------------------------
# Step 4 — patch training_features in parallel by month
# ---------------------------------------------------------------------------

def patch_month(ym: str):
    year, month = int(ym[:4]), int(ym[5:])
    month_start = f"{year}-{month:02d}-01"
    month_end   = f"{year + 1}-01-01" if month == 12 else f"{year}-{month + 1:02d}-01"

    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE training_features tf
        SET
            nearest_entrance_dist_m = p.nearest_entrance_dist_m,
            entrance_count_400m     = p.entrance_count_400m,
            entrance_count_800m     = p.entrance_count_800m,
            is_within_400m          = p.is_within_400m
        FROM citibike_station_subway_proximity p
        WHERE tf.station_id             = p.citibike_station_id
          AND tf.nearest_entrance_dist_m IS NULL
          AND tf.timestamp >= %s
          AND tf.timestamp <  %s
    """, (month_start, month_end))
    updated = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    print(f"  {ym}: {updated} rows updated")
    return updated


def patch_training_features(workers: int):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT DISTINCT TO_CHAR(DATE_TRUNC('month', timestamp), 'YYYY-MM')
        FROM training_features
        WHERE nearest_entrance_dist_m IS NULL
        ORDER BY 1
    """)
    months = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    if not months:
        print("  No rows with NULL proximity — nothing to patch")
        return

    print(f"  Patching {len(months)} months with {workers} workers...")
    with Pool(workers) as pool:
        results = pool.map(patch_month, months)
    print(f"  Total rows updated: {sum(results)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4,
                        help="parallel workers for CSV parsing and training_features patch (default 4)")
    parser.add_argument("--skip-wayback", action="store_true",
                        help="skip Wayback Machine fetches (use trip-CSV coords only)")
    args = parser.parse_args()

    print("Step 1 — extracting legacy station coords from 2019 trip CSVs...")
    trip_coords = extract_legacy_coords(ZIP_2019, args.workers)

    if args.skip_wayback:
        legacy_coords = trip_coords
        print("  Skipping Wayback fetch (--skip-wayback)")
    else:
        print("Step 1b — fetching additional coords from Wayback Machine GBFS snapshots...")
        wayback_coords = fetch_wayback_coords()

        # Union: trip-CSV coords take priority; Wayback fills in the gaps.
        trip_ids      = set(trip_coords["station_id"])
        new_from_wb   = wayback_coords[~wayback_coords["station_id"].isin(trip_ids)]
        legacy_coords = pd.concat([trip_coords, new_from_wb], ignore_index=True)
        print(f"  Combined: {len(trip_coords)} trip-CSV + {len(new_from_wb)} Wayback-only "
              f"= {len(legacy_coords)} total stations")

    conn = get_conn()
    cur  = conn.cursor()

    print("Step 2 — building station_id_crosswalk...")
    build_crosswalk(cur, legacy_coords)
    conn.commit()

    print("Step 3 — computing MTA proximity for legacy stations...")
    compute_proximity(cur, legacy_coords)
    conn.commit()

    cur.close()
    conn.close()

    print(f"Step 4 — patching training_features ({args.workers} workers)...")
    patch_training_features(args.workers)

    print("Done.")


if __name__ == "__main__":
    main()
