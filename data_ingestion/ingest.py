"""
Citibike GBFS live data ingestion script.

Long-running: starts once (via Task Scheduler on login) and loops forever,
polling station_status every 2.5 minutes. Reuses a single DB connection;
reconnects automatically on failure.

On startup it checks for missed poll windows since the last recorded
fetched_at and logs them to ingestion_log (no backfill — GBFS is live-only).
Station information is refreshed once per day.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

STATION_STATUS_URL    = "https://gbfs.citibikenyc.com/gbfs/en/station_status.json"
STATION_INFO_URL      = "https://gbfs.citibikenyc.com/gbfs/en/station_information.json"

POLL_INTERVAL_SECONDS = 150   # 2.5 minutes
INFO_REFRESH_HOURS    = 24
REQUEST_TIMEOUT       = 30    # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "ingest.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=int(os.environ.get("PGPORT", 5432)),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


def ensure_connection(conn):
    """Return conn if healthy, otherwise close and reconnect."""
    try:
        conn.cursor().execute("SELECT 1")
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        log.info("Reconnecting to database...")
        return get_connection()


def log_to_db(cur, status, fetched_at=None, station_count=None, error_message=None):
    cur.execute(
        """
        INSERT INTO ingestion_log (fetched_at, status, station_count, error_message)
        VALUES (%s, %s, %s, %s)
        """,
        (fetched_at, status, station_count, error_message),
    )


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def check_for_gaps(cur, now: datetime):
    """Log any missed 2.5-minute poll windows since the last recorded fetched_at."""
    cur.execute("SELECT MAX(fetched_at) FROM station_status")
    row = cur.fetchone()
    last = row[0] if row and row[0] else None

    if last is None:
        return  # first ever run, nothing to compare

    last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last
    gap_minutes = (now - last).total_seconds() / 60

    if gap_minutes <= (POLL_INTERVAL_SECONDS / 60) + 1:
        return  # within normal tolerance

    missed_start = last + timedelta(seconds=POLL_INTERVAL_SECONDS)
    slot = missed_start
    while slot < now - timedelta(seconds=POLL_INTERVAL_SECONDS):
        log.warning("Missed poll window: %s", slot.isoformat())
        log_to_db(cur, status="missed", fetched_at=slot,
                  error_message="Poll window missed (computer off or script not running)")
        slot += timedelta(seconds=POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Station information (daily refresh)
# ---------------------------------------------------------------------------

def should_refresh_station_info(cur) -> bool:
    cur.execute("SELECT MAX(last_updated) FROM station_information")
    row = cur.fetchone()
    if not row or row[0] is None:
        return True
    last = row[0].replace(tzinfo=timezone.utc) if row[0].tzinfo is None else row[0]
    return (datetime.now(timezone.utc) - last).total_seconds() > INFO_REFRESH_HOURS * 3600


def fetch_station_information() -> dict:
    resp = requests.get(STATION_INFO_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def upsert_station_information(cur, payload: dict):
    last_updated = datetime.fromtimestamp(payload["last_updated"], tz=timezone.utc)
    stations = payload["data"]["stations"]

    rows = []
    for s in stations:
        uris = s.get("rental_uris") or {}
        services = s.get("eightd_station_services")
        rows.append((
            s.get("station_id"),
            s.get("name"),
            s.get("short_name"),
            s.get("lat"),
            s.get("lon"),
            s.get("capacity"),
            str(s.get("region_id")) if s.get("region_id") is not None else None,
            s.get("station_type"),
            s.get("has_kiosk"),
            s.get("electric_bike_surcharge_waiver"),
            s.get("eightd_has_key_dispenser"),
            s.get("rental_methods"),
            uris.get("ios"),
            uris.get("android"),
            json.dumps(services) if services is not None else None,
            s.get("external_id"),
            last_updated,
        ))

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO station_information (
            station_id, name, short_name, lat, lon, capacity, region_id,
            station_type, has_kiosk, electric_bike_surcharge_waiver,
            eightd_has_key_dispenser, rental_methods, rental_uris_ios,
            rental_uris_android, eightd_station_services, external_id, last_updated
        ) VALUES %s
        ON CONFLICT (station_id) DO UPDATE SET
            name                          = EXCLUDED.name,
            short_name                    = EXCLUDED.short_name,
            lat                           = EXCLUDED.lat,
            lon                           = EXCLUDED.lon,
            capacity                      = EXCLUDED.capacity,
            region_id                     = EXCLUDED.region_id,
            station_type                  = EXCLUDED.station_type,
            has_kiosk                     = EXCLUDED.has_kiosk,
            electric_bike_surcharge_waiver = EXCLUDED.electric_bike_surcharge_waiver,
            eightd_has_key_dispenser      = EXCLUDED.eightd_has_key_dispenser,
            rental_methods                = EXCLUDED.rental_methods,
            rental_uris_ios               = EXCLUDED.rental_uris_ios,
            rental_uris_android           = EXCLUDED.rental_uris_android,
            eightd_station_services       = EXCLUDED.eightd_station_services,
            external_id                   = EXCLUDED.external_id,
            last_updated                  = EXCLUDED.last_updated
        """,
        rows,
    )
    log.info("Upserted %d station_information rows", len(rows))


# ---------------------------------------------------------------------------
# Station status (every poll)
# ---------------------------------------------------------------------------

def fetch_station_status() -> dict:
    resp = requests.get(STATION_STATUS_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def insert_station_status(cur, payload: dict) -> tuple[datetime, int]:
    fetched_at = datetime.fromtimestamp(payload["last_updated"], tz=timezone.utc)

    cur.execute("SELECT 1 FROM station_status WHERE fetched_at = %s LIMIT 1", (fetched_at,))
    if cur.fetchone():
        log.info("Feed last_updated %s already recorded — skipping insert", fetched_at.isoformat())
        return fetched_at, 0

    stations = payload["data"]["stations"]
    rows = []
    for s in stations:
        rows.append((
            fetched_at,
            s.get("station_id"),
            s.get("num_bikes_available"),
            s.get("num_ebikes_available"),
            s.get("num_bikes_disabled"),
            s.get("num_docks_available"),
            s.get("num_docks_disabled"),
            s.get("num_scooters_available"),
            s.get("num_scooters_unavailable"),
            s.get("is_installed"),
            s.get("is_renting"),
            s.get("is_returning"),
            s.get("eightd_has_available_keys"),
            s.get("last_reported"),
        ))

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO station_status (
            fetched_at, station_id,
            num_bikes_available, num_ebikes_available, num_bikes_disabled,
            num_docks_available, num_docks_disabled,
            num_scooters_available, num_scooters_unavailable,
            is_installed, is_renting, is_returning,
            eightd_has_available_keys, last_reported
        ) VALUES %s
        """,
        rows,
    )
    return fetched_at, len(rows)


# ---------------------------------------------------------------------------
# Single poll cycle
# ---------------------------------------------------------------------------

def poll(conn):
    now = datetime.now(timezone.utc)
    with conn:
        with conn.cursor() as cur:
            check_for_gaps(cur, now)

            if should_refresh_station_info(cur):
                log.info("Refreshing station_information...")
                info_payload = fetch_station_information()
                upsert_station_information(cur, info_payload)

            status_payload = fetch_station_status()
            fetched_at, count = insert_station_status(cur, status_payload)

            log_to_db(cur, status="success", fetched_at=fetched_at, station_count=count)
            log.info("Inserted %d station_status rows for %s", count, fetched_at.isoformat())


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    log.info("Ingest process started. Polling every %.1f minutes.", POLL_INTERVAL_SECONDS / 60)

    # Retry initial DB connection — Docker may still be starting at logon time.
    conn = None
    while conn is None:
        try:
            conn = get_connection()
            log.info("Database connected.")
        except Exception as exc:
            log.warning("DB connection failed, retrying in 30s: %s", exc)
            time.sleep(30)

    while True:
        try:
            conn = ensure_connection(conn)
            poll(conn)
        except requests.RequestException as exc:
            log.error("HTTP fetch failed: %s", exc)
            try:
                with conn:
                    with conn.cursor() as cur:
                        log_to_db(cur, status="error", error_message=str(exc))
            except Exception:
                pass
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
            try:
                with conn:
                    with conn.cursor() as cur:
                        log_to_db(cur, status="error", error_message=str(exc))
            except Exception:
                pass

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
