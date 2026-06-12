# %% Table row counts
from db import conn
cur = conn.cursor()

cur.execute("""
    select * from station_information
""")
print(cur.fetchall())

# %% Ingestion log — last 10 entries
cur.execute("""
    SELECT id, logged_at, fetched_at, status, station_count, error_message
    FROM ingestion_log
    ORDER BY logged_at DESC
    LIMIT 10
""")
print(cur.fetchall())

# %% Station status — 10 rows from latest snapshot
cur.execute("""
    SELECT ss.fetched_at, ss.station_id, si.name,
           ss.num_bikes_available, ss.num_ebikes_available,
           ss.num_docks_available, ss.is_renting, ss.is_returning
    FROM station_status ss
    LEFT JOIN station_information si USING (station_id)
    WHERE ss.fetched_at = (SELECT MAX(fetched_at) FROM station_status)
    ORDER BY si.name
    LIMIT 10
""")
print(cur.fetchall())

# %% Station information — 10 rows
cur.execute("""
    SELECT station_id, name, lat, lon, capacity, station_type, has_kiosk, last_updated
    FROM station_information
    ORDER BY name
    LIMIT 10
""")
print(cur.fetchall())

# %% Coverage summary
cur.execute("""
    SELECT MIN(fetched_at) AS earliest, MAX(fetched_at) AS latest,
           COUNT(DISTINCT fetched_at) AS distinct_polls, COUNT(*) AS total_rows
    FROM station_status
""")
print(cur.fetchall())
