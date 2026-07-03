import os
import json
import urllib.request
import psycopg2
from shapely.geometry import Point, shape
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Builds the station_borough lookup table using exact point-in-polygon matching
# against official NYC borough boundary polygons.
#
# Source: NYC Open Data borough boundaries (via GitHub mirror).
# Jersey City (region_id=70) and Hoboken (region_id=311) are assigned from
# station_information.region_id before the polygon check fires.
# Three Hoboken stations with NULL region_id are caught by lon < -74.020.
#
# Run once after any change to station_information, or when setting up on a
# new machine. Safe to re-run — TRUNCATE + reload each time.

GEOJSON_URL = (
    "https://raw.githubusercontent.com/dwillis/nyc-maps/master/boroughs.geojson"
)


def load_borough_shapes(url):
    with urllib.request.urlopen(url) as resp:
        geojson = json.load(resp)
    return [
        (feat["properties"]["BoroName"], shape(feat["geometry"]))
        for feat in geojson["features"]
    ]


def get_borough(lat, lon, borough_shapes):
    pt = Point(lon, lat)
    for name, polygon in borough_shapes:
        if polygon.contains(pt):
            return name
    return None


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def main():
    print("Downloading borough boundary GeoJSON...")
    borough_shapes = load_borough_shapes(GEOJSON_URL)
    print(f"  {len(borough_shapes)} borough polygons loaded.")

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT station_id, lat, lon, region_id FROM station_information "
            "WHERE lat IS NOT NULL"
        )
        stations = cur.fetchall()
    print(f"  {len(stations)} stations to assign.")

    rows = []
    unknown = []
    for sid, lat, lon, region_id in stations:
        if region_id == "70":
            borough = "Jersey City"
        elif region_id == "311" or (lon is not None and lon < -74.020):
            borough = "Hoboken"
        else:
            borough = get_borough(lat, lon, borough_shapes)
            if borough is None:
                unknown.append((sid, lat, lon))
                borough = "Unknown"
        rows.append((sid, borough))

    if unknown:
        print(f"  WARNING: {len(unknown)} stations outside all NYC borough polygons:")
        for sid, lat, lon in unknown:
            print(f"    {sid}  lat={lat}  lon={lon}")

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS station_borough (
                station_id  VARCHAR(255) PRIMARY KEY,
                borough     TEXT NOT NULL
            )
        """)
        cur.execute("TRUNCATE TABLE station_borough")
        cur.executemany(
            "INSERT INTO station_borough (station_id, borough) VALUES (%s, %s)",
            rows,
        )
    conn.commit()
    conn.close()

    from collections import Counter
    counts = Counter(b for _, b in rows)
    print("Done. Borough counts:")
    for borough, n in sorted(counts.items()):
        print(f"  {borough}: {n}")


if __name__ == "__main__":
    main()
