"""
Email alert delivery job.

Run every 30 minutes at :00 and :30 via Task Scheduler (CitibikeAlerts).
Sends one email per (email, station_id, date) — so a subscriber never gets
the same alert twice in one day.

Requires env vars (data_ingestion/.env):
  RESEND_API_KEY

GCP auth: data_ingestion/bigquery_key.json (service account key file).
"""

import argparse
import logging
import os
import sys
from datetime import datetime, date
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

try:
    import resend as resend_sdk
except ImportError:
    sys.exit("Missing package: pip install resend")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ET      = ZoneInfo("America/New_York")
PROJECT = "citibike-tableau-501513"
DATASET = "citibike"
GCP_KEY = os.path.join(os.path.dirname(__file__), "bigquery_key.json")

FROM_EMAIL = "BikePredict <alerts@bikepredict.fyi>"
APP_URL    = "https://bikepredict.fyi"
ANCHORS    = [60, 180, 360, 720, 1440, 2880]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def tbl(name):
    return f"`{PROJECT}.{DATASET}.{name}`"


# ---------------------------------------------------------------------------
# BigQuery helpers
# ---------------------------------------------------------------------------


def _bq_client():
    return bigquery.Client.from_service_account_json(GCP_KEY)


def _ensure_sent_alerts_table(client):
    client.query(f"""
        CREATE TABLE IF NOT EXISTS {tbl("sent_alerts")} (
            email       STRING  NOT NULL,
            station_id  STRING  NOT NULL,
            alert_date  DATE    NOT NULL,
            sent_at     TIMESTAMP
        )
    """).result()


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
        "predicted_value_lgbm":    lo["predicted_value_lgbm"]    * (1 - t) + hi["predicted_value_lgbm"]    * t,
        "pi_lower":                lo["pi_lower"]                * (1 - t) + hi["pi_lower"]                * t,
        "pi_upper":                lo["pi_upper"]                * (1 - t) + hi["pi_upper"]                * t,
    }


def _get_alerts_to_send(client, current_slot_et: str, test_mode: bool = False) -> list[dict]:
    # BigQuery does not allow correlated subqueries in JOIN predicates, so we
    # pre-compute the latest predicted_at per station in a CTE first.
    query = f"""
        WITH latest AS (
            SELECT station_id, MAX(predicted_at) AS max_predicted_at
            FROM   {tbl("model_predictions")}
            GROUP BY station_id
        )
        SELECT
            s.email,
            s.station_id,
            s.target_time,
            s.prediction_time,
            si.name                    AS station_name,
            mp.horizon_minutes,
            mp.predicted_prob_logistic,
            mp.predicted_value_lgbm,
            mp.pi_lower,
            mp.pi_upper
        FROM {tbl("subscribers")} s
        JOIN {tbl("station_information")} si
            ON si.station_id = s.station_id
        JOIN latest l
            ON l.station_id = s.station_id
        JOIN {tbl("model_predictions")} mp
            ON  mp.station_id   = s.station_id
            AND mp.predicted_at = l.max_predicted_at
        WHERE s.email           IS NOT NULL
          AND s.target_time     IS NOT NULL
          AND s.prediction_time IS NOT NULL
          AND (@test_mode OR s.target_time = @slot)
        ORDER BY s.email, s.station_id, mp.horizon_minutes
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("test_mode", "BOOL",   test_mode),
        bigquery.ScalarQueryParameter("slot",      "STRING", current_slot_et),
    ])
    result = client.query(query, job_config=job_config).result()
    return [dict(row) for row in result]


def _already_sent(client, email: str, station_id: str, today: date) -> bool:
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("email",      "STRING", email),
        bigquery.ScalarQueryParameter("station_id", "STRING", station_id),
        bigquery.ScalarQueryParameter("today",      "DATE",   today.isoformat()),
    ])
    result = client.query(
        f"SELECT 1 FROM {tbl('sent_alerts')} "
        "WHERE email = @email AND station_id = @station_id AND alert_date = @today LIMIT 1",
        job_config=job_config,
    ).result()
    return next(iter(result), None) is not None


def _record_sent(client, email: str, station_id: str, today: date):
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("email",      "STRING", email),
        bigquery.ScalarQueryParameter("station_id", "STRING", station_id),
        bigquery.ScalarQueryParameter("today",      "DATE",   today.isoformat()),
    ])
    client.query(
        f"INSERT INTO {tbl('sent_alerts')} (email, station_id, alert_date, sent_at) "
        "VALUES (@email, @station_id, @today, CURRENT_TIMESTAMP())",
        job_config=job_config,
    ).result()


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------


def _fmt_time(hhmm: str) -> str:
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
    prob_pct  = round(prediction["predicted_prob_logistic"] * 100)
    bikes     = round(prediction["predicted_value_lgbm"])
    pi_lo     = max(0, round(prediction["pi_lower"]))
    pi_hi     = round(prediction["pi_upper"])
    station   = sub["station_name"]
    alert_str = _fmt_time(sub["target_time"])
    pred_str  = _fmt_time(sub["prediction_time"])
    station_url = f"{APP_URL}/station/{sub['station_id']}"
    unsub_url   = f"{APP_URL}/signup?unsub_station={sub['station_id']}&unsub_email={quote(sub['email'])}"
    prob_color  = _prob_hex(prediction["predicted_prob_logistic"])

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
            "List-Unsubscribe": f"<{APP_URL}/signup?unsub_station={sub['station_id']}&unsub_email={quote(sub['email'])}>",
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
        help="Skip time filter and dedup — sends to all eligible subscribers immediately.",
    )
    args = parser.parse_args()

    now_et = datetime.now(ET)
    today  = now_et.date()
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

    client = _bq_client()
    _ensure_sent_alerts_table(client)

    rows = _get_alerts_to_send(client, current_slot_et, test_mode=args.test)
    log.info("%d prediction row(s) match slot %s", len(rows), current_slot_et)

    # Group by (email, station_id) — each signup inserts 6 rows (one per horizon)
    # but all need exactly ONE email per day.
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (row["email"], row["station_id"])
        if key not in grouped:
            grouped[key] = {"sub": row, "horizons": []}
        grouped[key]["horizons"].append(row)

    sent = skipped = errors = 0

    for (email, station_id), data in grouped.items():
        sub          = data["sub"]
        horizon_rows = data["horizons"]

        if not args.test and _already_sent(client, email, station_id, today):
            log.info("Skip (already sent today): %s / %s", email, sub.get("station_name"))
            skipped += 1
            continue

        target_min = (
            _hhmm_to_minutes(sub["prediction_time"])
            - _hhmm_to_minutes(sub["target_time"])
            + 1440
        ) % 1440

        prediction = _interpolate(horizon_rows, target_min)
        if not prediction:
            log.warning("No prediction data for %s / %s -- skipping", email, sub.get("station_name"))
            continue

        try:
            _send_email(sub, prediction)
            _record_sent(client, email, station_id, today)
            log.info(
                "Sent -> %s | %s | alert %s / pred %s | %d%%",
                email,
                sub.get("station_name"),
                sub["target_time"],
                sub["prediction_time"],
                round(prediction["predicted_prob_logistic"] * 100),
            )
            sent += 1
        except Exception as exc:
            log.error("Failed %s / %s: %s", email, sub.get("station_name"), exc)
            errors += 1

    log.info("Done -- %d sent, %d skipped (dup), %d errors", sent, skipped, errors)


if __name__ == "__main__":
    main()
