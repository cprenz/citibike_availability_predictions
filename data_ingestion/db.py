"""Simple psycopg2 connection helper for dev/utility scripts.

Credentials are loaded from data_ingestion/.env (gitignored) — never hardcode
them here. See .env.example for the required variables.
"""

import os

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

conn = psycopg2.connect(
    host=os.getenv("PGHOST", "localhost"),
    port=os.getenv("PGPORT", "5555"),
    dbname=os.getenv("PGDATABASE", "citibike"),
    user=os.getenv("PGUSER"),
    password=os.getenv("PGPASSWORD"),
)
