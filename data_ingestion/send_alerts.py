"""
Email alert delivery job.

Run every 30 minutes at :05 and :35 (after scoring :05, Snowflake sync :10).
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

import argparse
import logging
import os
import sys
from datetime import datetime, date
from typing import Optional
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
FROM_EMAIL = "BikePredict <alerts@bikepredict.fyi>"
APP_URL = "https://bikepredict.fyi"
ANCHORS = [60, 180, 360, 720, 1440, 2880]

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


def _hhmm_to_minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _interpolate(horizon_rows: list, target_minutes: int) -> Optional[dict]:
    """Linear interpolation between the two nearest anchor horizons."""
    clamped = max(ANCHORS[0], min(ANCHORS[-1], target_minutes))

    lower = upper = None
    for i in range(len(ANCHORS) - 1):
        if ANCHORS[i] <= clamped <= ANCHORS[i + 1]:
            lower, upper = ANCHORS[i], ANCHORS[i + 1]
            break
    if lower is None:
        lower = upper = ANCHORS[-1]

    lo = next((r for r in horizon_rows if r["horizon_minutes"] == lower), None)
    hi = next((r for r in horizon_rows if r["horizon_minutes"] == upper), None)

    if not lo and not hi:
        return None
    if not lo or lower == upper:
        return hi or lo
    if not hi:
        return lo

    t = (clamped - lower) / (upper - lower)
    return {
        "predicted_prob_logistic": lo["predicted_prob_logistic"] * (1 - t) + hi["predicted_prob_logistic"] * t,
        "predicted_value_lgbm": lo["predicted_value_lgbm"] * (1 - t) + hi["predicted_value_lgbm"] * t,
        "pi_lower": lo["pi_lower"] * (1 - t) + hi["pi_lower"] * t,
        "pi_upper": lo["pi_upper"] * (1 - t) + hi["pi_upper"] * t,
    }


def _get_alerts_to_send(cur, current_slot_et: str, test_mode: bool = False) -> list[dict]:
    """
    Return one row per (subscriber, horizon) where target_time exactly matches
    current_slot_et (HH:MM rounded to nearest 30min). Python side groups by
    subscriber and interpolates to the prediction_time the user chose.

    In test_mode, the time filter is skipped so all eligible subscribers fire.
    """
    hour_filter = "" if test_mode else "AND s.target_time = %s"
    query = f"""
        SELECT
            s.id                        AS subscriber_id,
            s.email,
            s.station_id,
            s.target_time,
            s.prediction_time,
            si.name                     AS station_name,
            mp.horizon_minutes,
            mp.predicted_prob_logistic,
            mp.predicted_value_lgbm,
            mp.pi_lower,
            mp.pi_upper
        FROM subscribers s
        JOIN station_information si
            ON  si.station_id = s.station_id
        JOIN model_predictions mp
            ON  mp.station_id   = s.station_id
            AND mp.predicted_at = (
                SELECT MAX(mp2.predicted_at)
                FROM   model_predictions mp2
                WHERE  mp2.station_id = s.station_id
            )
        WHERE s.email           IS NOT NULL
          AND s.target_time     IS NOT NULL
          AND s.prediction_time IS NOT NULL
          {hour_filter}
        ORDER BY s.id, mp.horizon_minutes
    """
    binds = () if test_mode else (current_slot_et,)
    cur.execute(query, binds)
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


def _build_email(sub: dict, prediction: dict) -> dict:
    prob_pct = round(prediction["predicted_prob_logistic"] * 100)
    bikes = round(prediction["predicted_value_lgbm"])
    pi_lo = max(0, round(prediction["pi_lower"]))
    pi_hi = round(prediction["pi_upper"])
    station = sub["station_name"]
    alert_str = _fmt_time(sub["target_time"])
    pred_str = _fmt_time(sub["prediction_time"])
    station_url = f"{APP_URL}/station/{sub['station_id']}"
    unsub_url = f"{APP_URL}/signup"
    prob_color = _prob_hex(prediction["predicted_prob_logistic"])

    subject = (
        f"Bike alert: {station} at {pred_str} -- "
        f"{prob_pct}% chance a bike is available"
    )

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:system-ui,sans-serif">
<div style="max-width:480px;margin:32px auto;background:#fff;border-radius:12px;
            overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)">

  <div style="background:#1e40af;padding:20px 28px">
    <span style="color:#fff;font-weight:700;font-size:16px">BikePredict</span>
  </div>

  <div style="padding:28px">
    <p style="margin:0 0 4px;font-size:18px;font-weight:700;color:#111">{station}</p>
    <p style="margin:0 0 20px;font-size:14px;color:#6b7280">
      Predicted availability at <strong>{pred_str}</strong>
      &nbsp;&middot;&nbsp; Alert sent at {alert_str}
    </p>

    <div style="background:#f9fafb;border-radius:10px;padding:20px;margin-bottom:24px">
      <div style="font-size:40px;font-weight:700;color:#111;line-height:1">{bikes}</div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px">predicted bikes</div>
      <div style="margin-top:14px;font-size:24px;font-weight:700;color:{prob_color}">
        {prob_pct}%
      </div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px">
        probability at least one bike is available at {pred_str}
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
    You signed up for Citi Bike availability alerts at bikepredict.fyi.<br>
    Not affiliated with Citi Bike, Lyft, or NYC Bike Share.<br>
    <a href="{unsub_url}" style="color:#9ca3af">Unsubscribe</a>
    &nbsp;&bull;&nbsp; New York, NY
  </div>
</div>
</body>
</html>"""

    plain = (
        f"BikePredict -- {station} at {pred_str}\n\n"
        f"{bikes} bikes predicted | {prob_pct}% probability a bike is available\n"
        f"95% range: {pi_lo} to {pi_hi} bikes\n\n"
        f"Full forecast: {station_url}\n\n"
        f"Unsubscribe: {unsub_url}\n"
        f"New York, NY"
    )

    return {"subject": subject, "html": html, "text": plain}


def _send_email(sub: dict, prediction: dict):
    resend_sdk.api_key = os.environ["RESEND_API_KEY"]
    content = _build_email(sub, prediction)
    resend_sdk.Emails.send({
        "from": FROM_EMAIL,
        "to": sub["email"],
        "subject": content["subject"],
        "html": content["html"],
        "text": content["text"],
        "headers": {
            # Gmail shows a native unsubscribe button when this header is present
            "List-Unsubscribe": f"<{APP_URL}/signup>",
        },
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test",
        action="store_true",
        help="Skip hour filter and dedup check — sends to all eligible subscribers immediately.",
    )
    args = parser.parse_args()

    now_et = datetime.now(ET)
    today = now_et.date()
    # Round to nearest 30-min slot (00 or 30)
    slot_min = 0 if now_et.minute < 30 else 30
    current_slot_et = f"{now_et.hour:02d}:{slot_min:02d}"
    log.info(
        "Alert job starting -- %s ET (slot %s)%s",
        now_et.strftime("%Y-%m-%d %H:%M"),
        current_slot_et,
        " [TEST MODE]" if args.test else "",
    )

    if "RESEND_API_KEY" not in os.environ:
        log.error("RESEND_API_KEY not set -- cannot send emails. Exiting.")
        sys.exit(1)

    conn = _snowflake_conn()
    cur = conn.cursor()

    try:
        _ensure_sent_alerts_table(cur)

        rows = _get_alerts_to_send(cur, current_slot_et, test_mode=args.test)
        log.info("%d prediction row(s) match slot %s", len(rows), current_slot_et)

        # Group by subscriber_id — each subscriber has 6 horizon rows
        grouped: dict[int, dict] = {}
        for row in rows:
            sub_id = int(row["subscriber_id"])
            if sub_id not in grouped:
                grouped[sub_id] = {"sub": row, "horizons": []}
            grouped[sub_id]["horizons"].append(row)

        sent = skipped = errors = 0

        for sub_id, data in grouped.items():
            sub = data["sub"]
            horizon_rows = data["horizons"]

            if not args.test and _already_sent(cur, sub_id, today):
                log.info(
                    "Skip (already sent today): sub %d / %s",
                    sub_id,
                    sub["station_name"],
                )
                skipped += 1
                continue

            # Compute target horizon: how far ahead is prediction_time from target_time?
            target_min = (
                _hhmm_to_minutes(sub["prediction_time"])
                - _hhmm_to_minutes(sub["target_time"])
                + 1440
            ) % 1440

            prediction = _interpolate(horizon_rows, target_min)
            if not prediction:
                log.warning(
                    "No prediction data for sub %d (%s) -- skipping",
                    sub_id,
                    sub["station_name"],
                )
                continue

            try:
                _send_email(sub, prediction)
                _record_sent(cur, sub_id, today)
                log.info(
                    "Sent -> %s | %s | alert %s / pred %s | %d%%",
                    sub["email"],
                    sub["station_name"],
                    sub["target_time"],
                    sub["prediction_time"],
                    round(prediction["predicted_prob_logistic"] * 100),
                )
                sent += 1
            except Exception as exc:
                log.error("Failed sub %d (%s): %s", sub_id, sub["email"], exc)
                errors += 1

        log.info("Done -- %d sent, %d skipped (dup), %d errors", sent, skipped, errors)

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
