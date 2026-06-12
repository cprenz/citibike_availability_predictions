import os
import numpy as np
import psycopg2
from dotenv import load_dotenv
from sklearn.neighbors import BallTree

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'data_ingestion', '.env'))

EARTH_RADIUS_M = 6_371_000
RADIUS_400M = 400 / EARTH_RADIUS_M   # radians
RADIUS_800M = 800 / EARTH_RADIUS_M   # radians


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def main():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT station_id, lat, lon FROM station_information WHERE lat IS NOT NULL AND lon IS NOT NULL")
    citibike_rows = cur.fetchall()
    cb_ids = [r[0] for r in citibike_rows]
    cb_coords = np.radians([[float(r[1]), float(r[2])] for r in citibike_rows])

    cur.execute("SELECT entrance_latitude, entrance_longitude FROM mta_subway_entrances")
    mta_rows = cur.fetchall()
    if not mta_rows:
        print("mta_subway_entrances is empty — run fetch_mta_entrances.py first.")
        return
    mta_coords = np.radians([[float(r[0]), float(r[1])] for r in mta_rows])

    print(f"Building BallTree from {len(mta_coords)} MTA entrances...")
    tree = BallTree(mta_coords, metric="haversine")

    print(f"Querying proximity for {len(cb_ids)} Citibike stations...")
    dist_nearest, _ = tree.query(cb_coords, k=1)
    counts_400 = tree.query_radius(cb_coords, r=RADIUS_400M, count_only=True)
    counts_800 = tree.query_radius(cb_coords, r=RADIUS_800M, count_only=True)

    records = []
    for i, station_id in enumerate(cb_ids):
        dist_m = float(dist_nearest[i][0]) * EARTH_RADIUS_M
        c400 = int(counts_400[i])
        c800 = int(counts_800[i])
        nearest = float(dist_m) if c800 > 0 else None
        records.append((
            str(station_id),
            nearest,
            c400,
            c800,
            bool(c400 > 0),
        ))

    cur.executemany("""
        INSERT INTO citibike_station_subway_proximity (
            citibike_station_id, nearest_entrance_dist_m,
            entrance_count_400m, entrance_count_800m, is_within_400m
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (citibike_station_id) DO UPDATE SET
            nearest_entrance_dist_m = EXCLUDED.nearest_entrance_dist_m,
            entrance_count_400m     = EXCLUDED.entrance_count_400m,
            entrance_count_800m     = EXCLUDED.entrance_count_800m,
            is_within_400m          = EXCLUDED.is_within_400m
    """, records)
    conn.commit()
    conn.close()
    print(f"Done. {len(records)} rows upserted into citibike_station_subway_proximity.")


if __name__ == "__main__":
    main()
