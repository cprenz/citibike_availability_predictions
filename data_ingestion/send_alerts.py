"""
Hourly email alert delivery job.

Run at :15 each hour (after scoring :05, Snowflake sync :10).
Sends one email per (subscriber_id, date) so a subscriber never gets the
same alert twice in one day. Only fires for 1hr and 3hr horizons — the most
actionable lead times for a commuter.

Requires env vars (same .env as the other ingestion scripts):
  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA,
  SNOWFLAKE_WAREHOUSE, RESEND_API_KEY

Optional:
  SNOWFLAKE_PRIVATE_KEY — PEM content with literal \\n (Vercel style).
  Falls back to data_ingestion/snowflake_key.p8 if not set.

BigQuery migration (future): swap the _snowflake_conn() block only.
Everything else — Resend call, email HTML, dedup logic — stays identical.
"""

import logging
import os
import sys
from datetime import datetime, date
from zoneinfo import ZoneInfo

import snowflake.connector
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# pip install resend
try:
    import resend as resend_sdk
except ImportError:
    sys.exit("Missing package: pip install resend")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
FROM_EMAIL = "Citi Bike Predictions <alerts@citibikepredictions.com>"
APP_URL = "https://citibike-availability-predictions.vercel.app"
# Only 1hr and 3hr for MVP — close enough to the target time to be actionable
ACTIVE_HORIZONS = (60, 180)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Snowflake helpers
# ---------------------------------------------------------------------------


def _load_private_key():
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY")
    if pem:
        key_bytes = pem.replace("\\n", "\n").encode()
    else:
        key_path = os.path.join(os.path.dirname(__file__), "snowflake_key.p8")
        with open(key_path, "rb") as f:
            key_bytes = f.read()
    return load_pem_private_key(key_bytes, password=None)


def _snowflake_conn():
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        private_key=_load_private_key(),
        database=os.environ.get("SNOWFLAKE_DATABASE", "CITIBIKE"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
    )


def _ensure_sent_alerts_table(cur):
    """Create deduplication table if it doesn't exist yet."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_alerts (
            subscriber_id  INTEGER      NOT NULL,
            alert_date     DATE         NOT NULL,
            sent_at        TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _get_alerts_to_send(cur, current_hour_et: int) -> list[dict]:
    """
    Return subscriber rows whose alert should fire at this ET hour.

    Alert fires when floor((target_time_minutes - horizon_minutes) % 1440 / 60)
    equals the current ET hour. Example: target_time='08:00', horizon=180 fires
    at floor((480 - 180 + 1440) % 1440 / 60) = floor(300 / 60) = 5 (5 AM ET).

    Joins to the most recent model_predictions row per station+horizon.
    """
    query = """
        WITH base AS (
            SELECT
                s.id                        AS subscriber_id,
                s.email,
                s.station_id,
                s.target_time,
                s.horizon_minutes,
                si.name                     AS station_name,
                mp.predicted_prob_logistic,
                mp.predicted_value_lgbm,
                mp.pi_lower,
                mp.pi_upper,
                MOD(
                    TO_NUMBER(SPLIT_PART(s.target_time, ':', 1)) * 60
                    + TO_NUMBER(SPLIT_PART(s.target_time, ':', 2))
                    - s.horizon_minutes + 1440,
                    1440
                ) AS alert_minutes
            FROM subscribers s
            JOIN station_information si
                ON  si.station_id = s.station_id
            JOIN model_predictions mp
                ON  mp.station_id      = s.station_id
                AND mp.horizon_minutes = s.horizon_minutes
                AND mp.predicted_at    = (
                    SELECT MAX(mp2.predicted_at)
                    FROM   model_predictions mp2
                    WHERE  mp2.station_id      = s.station_id
                      AND  mp2.horizon_minutes = s.horizon_minutes
                )
            WHERE s.email           IS NOT NULL
              AND s.target_time     IS NOT NULL
              AND s.horizon_minutes IN (60, 180)
        )
        SELECT *
        FROM   base
        WHERE  FLOOR(alert_minutes / 60) = %s
        ORDER  BY email, station_id
    """
    cur.execute(query, (current_hour_et,))
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _already_sent(cur, subscriber_id: int, today: date) -> bool:
    cur.execute(
        "SELECT 1 FROM sent_alerts WHERE subscriber_id = %s AND alert_date = %s LIMIT 1",
        (subscriber_id, today.isoformat()),
    )
    return cur.fetchone() is not None


def _record_sent(cur, subscriber_id: int, today: date):
    cur.execute(
        "INSERT INTO sent_alerts (subscriber_id, alert_date) VALUES (%s, %s)",
        (subscriber_id, today.isoformat()),
    )


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------


def _fmt_time(hhmm: str) -> str:
    """'08:00' -> '8:00 AM', '13:30' -> '1:30 PM'"""
    h, m = int(hhmm[:2]), int(hhmm[3:])
    period = "AM" if h < 12 else "PM"
    display_h = h % 12 or 12
    return f"{display_h}:{m:02d} {period}"


def _prob_hex(prob: float) -> str:
    if prob >= 0.7:
        return "#16a34a"
    if prob >= 0.4:
        return "#d97706"
    return "#dc2626"


def _build_email(alert: dict) -> dict:
    prob_pct = round(alert["predicted_prob_logistic"] * 100)
    bikes = round(alert["predicted_value_lgbm"])
    pi_lo = max(0, round(alert["pi_lower"]))
    pi_hi = round(alert["pi_upper"])
    station = alert["station_name"]
    target_str = _fmt_time(alert["target_time"])
    station_url = f"{APP_URL}/station/{alert['station_id']}"
    unsub_url = f"{APP_URL}/unsubscribe?id={alert['subscriber_id']}"
    prob_color = _prob_hex(alert["predicted_prob_logistic"])

    subject = (
        f"Heads up: {station} at {target_str} -- "
        f"{prob_pct}% probability a bike is available"
    )

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:system-ui,sans-serif">
<div style="max-width:480px;margin:32px auto;background:#fff;border-radius:12px;
            overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)">

  <div style="background:#1e40af;padding:20px 28px">
    <span style="color:#fff;font-weight:700;font-size:16px">Citi Bike Predictions</span>
  </div>

  <div style="padding:28px">
    <p style="margin:0 0 4px;font-size:18px;font-weight:700;color:#111">{station}</p>
    <p style="margin:0 0 20px;font-size:14px;color:#6b7280">Alert for {target_str}</p>

    <div style="background:#f9fafb;border-radius:10px;padding:20px;margin-bottom:24px">
      <div style="font-size:40px;font-weight:700;color:#111;line-height:1">{bikes}</div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px">predicted bikes</div>
      <div style="margin-top:14px;font-size:24px;font-weight:700;color:{prob_color}">
        {prob_pct}%
      </div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px">
        probability at least one bike is available
      </div>
      <div style="margin-top:8px;font-size:12px;color:#9ca3af">
        95% range: {pi_lo} to {pi_hi} bikes
      </div>
    </div>

    <a href="{station_url}"
       style="display:inline-block;padding:12px 22px;background:#2563eb;color:#fff;
              border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">
      See full forecast
    </a>
  </div>

  <div style="padding:16px 28px;border-top:1px solid #e5e7eb;
              font-size:12px;color:#9ca3af;line-height:1.6">
    You signed up for Citi Bike availability alerts.<br>
    <a href="{unsub_url}" style="color:#9ca3af">Unsubscribe</a>
    &nbsp;&bull;&nbsp;Clark Prenz, New York, NY
  </div>
</div>
</body>
</html>"""

    plain = (
        f"Citi Bike Alert -- {station} at {target_str}\n\n"
        f"{bikes} bikes predicted | {prob_pct}% probability a bike is available\n"
        f"95% range: {pi_lo} to {pi_hi} bikes\n\n"
        f"Full forecast: {station_url}\n\n"
        f"Unsubscribe: {unsub_url}\n"
        f"Clark Prenz, New York, NY"
    )

    return {"subject": subject, "html": html, "text": plain}


def _send_email(alert: dict):
    resend_sdk.api_key = os.environ["RESEND_API_KEY"]
    email = _build_email(alert)
    resend_sdk.Emails.send({
        "from": FROM_EMAIL,
        "to": alert["email"],
        "subject": email["subject"],
        "html": email["html"],
        "text": email["text"],
        "headers": {
            # Gmail shows a native unsubscribe button when this header is present
            "List-Unsubscribe": (
                f"<{APP_URL}/unsubscribe?id={alert['subscriber_id']}>"
            ),
        },
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    now_et = datetime.now(ET)
    today = now_et.date()
    log.info(
        "Alert job starting -- %s ET (hour %d)",
        now_et.strftime("%Y-%m-%d %H:%M"),
        now_et.hour,
    )

    if "RESEND_API_KEY" not in os.environ:
        log.error("RESEND_API_KEY not set -- cannot send emails. Exiting.")
        sys.exit(1)

    conn = _snowflake_conn()
    cur = conn.cursor()

    try:
        _ensure_sent_alerts_table(cur)

        alerts = _get_alerts_to_send(cur, now_et.hour)
        log.info("%d subscriber row(s) match hour %d", len(alerts), now_et.hour)

        sent = skipped = errors = 0

        for alert in alerts:
            sub_id = int(alert["subscriber_id"])

            if _already_sent(cur, sub_id, today):
                log.info(
                    "Skip (already sent today): sub %d / %s",
                    sub_id,
                    alert["station_name"],
                )
                skipped += 1
                continue

            try:
                _send_email(alert)
                _record_sent(cur, sub_id, today)
                log.info(
                    "Sent -> %s | %s at %s | %d%%",
                    alert["email"],
                    alert["station_name"],
                    alert["target_time"],
                    round(alert["predicted_prob_logistic"] * 100),
                )
                sent += 1
            except Exception as exc:
                log.error("Failed sub %d (%s): %s", sub_id, alert["email"], exc)
                errors += 1

        log.info("Done -- %d sent, %d skipped (dup), %d errors", sent, skipped, errors)

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
