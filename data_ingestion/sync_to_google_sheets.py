import os
import gspread
import pandas as pd
import snowflake.connector
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Syncs Snowflake summary views to individual Google Sheets (one sheet per view).
# Split into separate sheets because Tableau's Google Drive connector fails on
# files over ~10MB — the old single-workbook approach hit that limit.
# Run daily after the Snowflake daily sync (10pm) — scheduled at 10:30pm via Task Scheduler.

KEY_FILE = os.path.join(os.path.dirname(__file__), "google_sheets_key.json")
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# One sheet per view: {sheet_id: (sheet_name, SQL query)}
# station_monthly is split by year so each sheet stays under Tableau's ~10MB export limit.
SHEETS = {
    "1GoaadWp7T2ypZuB5yMIcr6MXY1rdR9VeXssPT2UFt_U": (
        "citibike daily totals",
        "SELECT * FROM citibike_daily_totals ORDER BY date, borough",
    ),
    "19Yi3qvov6sQ6uCO_WIytxJ1ENckidPePCxIp0NQnzrM": (
        "citibike station summary",
        "SELECT * FROM citibike_station_summary ORDER BY total_rides DESC",
    ),
    "1vWhL_yR0P1ezz0sh6ImzO_NCdPTpprB9V0hFas9sZtU": (
        "citibike hourly profile",
        "SELECT * FROM citibike_hourly_profile ORDER BY hour_of_day",
    ),
    "1iMcKFxYmTAi4o9LayPLKW2WiutRKkiwvqb_svt1sCFo": (
        "citibike station monthly status",
        "SELECT * FROM citibike_station_monthly_status ORDER BY month, station_id",
    ),
    "1FFa6uFu3ETBTlC3Is4dcWzA_OAb-Ofmer4-3By3giMc": (
        "citibike station monthly 2019",
        "SELECT * FROM citibike_station_monthly WHERE YEAR(month) = 2019 ORDER BY month, station_id",
    ),
    "1BFUMtWjHTai_DQ9jIrupGLMzaR1KTrvX48bSx-1_A3I": (
        "citibike station monthly 2020",
        "SELECT * FROM citibike_station_monthly WHERE YEAR(month) = 2020 ORDER BY month, station_id",
    ),
    "12j2cJQu73ylqEmHHK68yMUnbFzzF5XmA0Vht7jw_MK0": (
        "citibike station monthly 2021",
        "SELECT * FROM citibike_station_monthly WHERE YEAR(month) = 2021 ORDER BY month, station_id",
    ),
    "1H3hjLdyK2eWZkeX4T0wq59sX2DV-0GgAvH6b_GimF7k": (
        "citibike station monthly 2022",
        "SELECT * FROM citibike_station_monthly WHERE YEAR(month) = 2022 ORDER BY month, station_id",
    ),
    "1m3CxC-BcXNbeYamOLxEvdf-iM6GcZs0e59c4-ES1eus": (
        "citibike station monthly 2023",
        "SELECT * FROM citibike_station_monthly WHERE YEAR(month) = 2023 ORDER BY month, station_id",
    ),
    "1Bo7Wv8ZiYzjpVDHHLgG3b8HFXbOq6euUCoZF5udCU5A": (
        "citibike station monthly 2024",
        "SELECT * FROM citibike_station_monthly WHERE YEAR(month) = 2024 ORDER BY month, station_id",
    ),
    "1jD2iBEarFpg6UE856v-VmLpWpH-eN-xKW8ICZ2AW3Cw": (
        "citibike station monthly 2025",
        "SELECT * FROM citibike_station_monthly WHERE YEAR(month) = 2025 ORDER BY month, station_id",
    ),
    "1ClTVIKhVTQwGq0AeyiGaEb5c8WdShoxoPVPHRNW543Y": (
        "citibike station monthly 2026",
        "SELECT * FROM citibike_station_monthly WHERE YEAR(month) = 2026 ORDER BY month, station_id",
    ),
}


def sf_conn():
    key_path = os.path.join(os.path.dirname(__file__), "snowflake_key.p8")
    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    private_key_bytes = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        private_key=private_key_bytes,
        database=os.getenv("SNOWFLAKE_DATABASE", "CITIBIKE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
    )


def push_to_sheet(gc, sheet_id, sheet_name, df):
    sh = gc.open_by_key(sheet_id)
    ws = sh.sheet1
    ws.clear()
    # Resize to fit data (header + data rows, all columns)
    ws.resize(rows=len(df) + 1, cols=len(df.columns))
    rows = [df.columns.tolist()] + df.astype(str).values.tolist()
    batch_size = 5000
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        ws.update(batch, f'A{i + 1}')
        print(f"      rows {i + 1}-{i + len(batch)}")


def main():
    print(f"Starting Google Sheets sync at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

    creds = Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    conn = sf_conn()

    try:
        for sheet_id, (sheet_name, query) in SHEETS.items():
            print(f"  Syncing '{sheet_name}'...")
            df = pd.read_sql(query, conn)
            print(f"    {len(df):,} rows pulled.")
            push_to_sheet(gc, sheet_id, sheet_name, df)
            print(f"    Done.")
    finally:
        conn.close()

    print("Google Sheets sync complete.")


if __name__ == "__main__":
    main()
