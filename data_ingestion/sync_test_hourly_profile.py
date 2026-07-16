import os
import sys
import gspread
import pandas as pd
import snowflake.connector
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# One-off diagnostic: writes ONLY citibike_hourly_profile (24 rows) to a
# small, user-owned test sheet. Purpose is to isolate whether the Tableau
# Public A7AE75CC error is caused by file size (large sheet exceeds Google
# Drive's xlsx export limit) rather than permissions. Pass the target sheet
# ID as the first command-line argument:
#   python sync_test_hourly_profile.py <SHEET_ID>

KEY_FILE = os.path.join(os.path.dirname(__file__), "google_sheets_key.json")
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

QUERY = "SELECT * FROM citibike_hourly_profile ORDER BY hour_of_day"


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


def main():
    if len(sys.argv) < 2:
        print("Usage: python sync_test_hourly_profile.py <SHEET_ID>")
        sys.exit(1)
    sheet_id = sys.argv[1].strip()

    print(f"Starting test sync at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

    creds = Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)

    conn = sf_conn()
    try:
        print("  Pulling citibike_hourly_profile from Snowflake...")
        df = pd.read_sql(QUERY, conn)
        print(f"    {len(df):,} rows pulled.")
    finally:
        conn.close()

    sh = gc.open_by_key(sheet_id)
    ws = sh.sheet1
    ws.clear()
    rows = [df.columns.tolist()] + df.astype(str).values.tolist()
    ws.update(rows, "A1")
    print(f"    Wrote {len(df)} rows to '{sh.title}' / tab '{ws.title}'.")
    print("Test sync complete. Now connect Tableau Public to this sheet.")


if __name__ == "__main__":
    main()
