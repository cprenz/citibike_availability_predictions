import { NextResponse } from "next/server";
import { BigQuery } from "@google-cloud/bigquery";
import path from "path";

const PROJECT = process.env.GCP_PROJECT_ID ?? "citibike-tableau-501513";
const DATASET  = process.env.GCP_DATASET    ?? "citibike";

const VALID_HORIZONS = new Set([60, 180, 360, 720, 1440, 2880]);

type SubscribeBody = {
  email?: string | null;
  phone?: string | null;
  station_id?: string;
  station_name?: string | null;
  target_time?: string | null;
  prediction_time?: string | null;
  horizons?: number[];
  threshold?: number | null;
};

function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function isValidTime(value: string): boolean {
  return /^([01]\d|2[0-3]):([0-5]\d)$/.test(value);
}

function formatTime(hhmm: string): string {
  const [hStr, mStr] = hhmm.split(":");
  const h = parseInt(hStr, 10);
  const m = mStr ?? "00";
  const period = h >= 12 ? "PM" : "AM";
  const displayH = h % 12 === 0 ? 12 : h % 12;
  return `${displayH}:${m} ${period}`;
}

function getBQ(): BigQuery {
  if (process.env.GCP_SERVICE_ACCOUNT_KEY) {
    return new BigQuery({
      projectId: PROJECT,
      credentials: JSON.parse(process.env.GCP_SERVICE_ACCOUNT_KEY),
    });
  }
  return new BigQuery({
    projectId: PROJECT,
    keyFilename: path.resolve(process.cwd(), "..", "data_ingestion", "bigquery_key.json"),
  });
}

async function sendConfirmationEmail(
  email: string,
  stationName: string | null,
  stationId: string,
  targetTime: string | null,
  predictionTime: string | null
): Promise<void> {
  const apiKey = process.env.RESEND_API_KEY;
  if (!apiKey) return;

  const stationLabel = stationName ?? stationId;
  const alertLabel   = targetTime     ? formatTime(targetTime)     : null;
  const predLabel    = predictionTime ? formatTime(predictionTime) : null;
  const stationUrl   = `https://bikepredict.fyi/station/${stationId}`;
  const unsubUrl     = `https://bikepredict.fyi/signup`;

  const html = `<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:system-ui,-apple-system,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 16px">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%">
        <tr>
          <td style="background:#1e40af;border-radius:10px 10px 0 0;padding:24px 32px">
            <span style="color:#fff;font-size:20px;font-weight:700">BikePredict</span>
            <span style="color:#93c5fd;font-size:13px;margin-left:12px">bikepredict.fyi</span>
          </td>
        </tr>
        <tr>
          <td style="background:#fff;padding:32px">
            <p style="margin:0 0 6px;font-size:22px;font-weight:700;color:#111">You&rsquo;re signed up for alerts</p>
            <p style="margin:0 0 24px;font-size:14px;color:#6b7280">Here&rsquo;s when you&rsquo;ll hear from us.</p>
            <table width="100%" cellpadding="0" cellspacing="0"
              style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:24px">
              <tr><td style="padding:20px 24px">
                <div style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;margin-bottom:4px">Station</div>
                <div style="font-size:16px;font-weight:700;color:#111">${stationLabel}</div>
                ${alertLabel ? `
                <div style="margin-top:14px;padding-top:14px;border-top:1px solid #e5e7eb">
                  <div style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;margin-bottom:4px">Alert time</div>
                  <div style="font-size:16px;font-weight:700;color:#111">${alertLabel} daily</div>
                </div>` : ""}
                ${predLabel ? `
                <div style="margin-top:14px;padding-top:14px;border-top:1px solid #e5e7eb">
                  <div style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;margin-bottom:4px">Predictions for</div>
                  <div style="font-size:16px;font-weight:700;color:#111">${predLabel}</div>
                </div>` : ""}
              </td></tr>
            </table>
            <table cellpadding="0" cellspacing="0">
              <tr><td style="background:#2563eb;border-radius:7px">
                <a href="${stationUrl}"
                   style="display:inline-block;padding:12px 24px;color:#fff;font-size:14px;font-weight:600;text-decoration:none">
                  See predictions for this station &rarr;
                </a>
              </td></tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#f9fafb;border-radius:0 0 10px 10px;padding:20px 32px;border-top:1px solid #e5e7eb">
            <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.6">
              You&rsquo;re receiving this because you signed up at bikepredict.fyi.<br>
              Not affiliated with Citi Bike, Lyft, or NYC Bike Share.<br>
              BikePredict &bull; New York, NY &bull;
              <a href="${unsubUrl}" style="color:#9ca3af;text-decoration:underline">Unsubscribe</a>
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>`;

  try {
    await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { "Authorization": `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: "BikePredict <alerts@bikepredict.fyi>",
        to: [email],
        subject: `You're signed up for alerts at ${stationLabel}`,
        html,
      }),
    });
  } catch (err) {
    console.error("Confirmation email failed (non-fatal):", err);
  }
}

export async function POST(request: Request) {
  let body: SubscribeBody;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const email         = body.email?.trim()          || null;
  const phone         = body.phone?.trim()          || null;
  const stationId     = body.station_id?.trim();
  const stationName   = body.station_name?.trim()   || null;
  const targetTime    = body.target_time?.trim()    || null;
  const predictionTime = body.prediction_time?.trim() || null;
  const horizons      = Array.isArray(body.horizons) ? body.horizons : [];
  const threshold     =
    typeof body.threshold === "number" && Number.isFinite(body.threshold)
      ? body.threshold
      : null;

  if (!email && !phone)
    return NextResponse.json({ error: "Provide at least an email or phone number." }, { status: 400 });
  if (email && !isValidEmail(email))
    return NextResponse.json({ error: "That email address doesn't look valid." }, { status: 400 });
  if (!stationId)
    return NextResponse.json({ error: "Please choose a station." }, { status: 400 });
  if (!targetTime)
    return NextResponse.json({ error: "Choose when you want the alert email." }, { status: 400 });
  if (!predictionTime)
    return NextResponse.json({ error: "Choose what time you want availability predictions for." }, { status: 400 });
  if (!isValidTime(targetTime))
    return NextResponse.json({ error: "Target time must be in HH:MM format." }, { status: 400 });
  if (!isValidTime(predictionTime))
    return NextResponse.json({ error: "Prediction time must be in HH:MM format." }, { status: 400 });

  const cleanHorizons = horizons.filter((h) => VALID_HORIZONS.has(h));
  if (cleanHorizons.length === 0)
    return NextResponse.json({ error: "Select at least one alert horizon." }, { status: 400 });

  const bq    = getBQ();
  const table = `\`${PROJECT}.${DATASET}.subscribers\``;

  try {
    // BigQuery has no multi-row DML VALUES — run one INSERT per horizon.
    // At 6 rows this is fast enough; streaming API is avoided so DELETE works immediately.
    for (const h of cleanHorizons) {
      await bq.query({
        query: `
          INSERT INTO ${table}
            (email, phone, station_id, station_name, target_time, prediction_time, horizon_minutes, threshold, created_at)
          VALUES
            (@email, @phone, @station_id, @station_name, @target_time, @prediction_time, @horizon_minutes, @threshold, CURRENT_TIMESTAMP())
        `,
        params: {
          email,
          phone,
          station_id:      stationId,
          station_name:    stationName,
          target_time:     targetTime,
          prediction_time: predictionTime,
          horizon_minutes: h,
          threshold,
        },
        types: {
          email:           "STRING",
          phone:           "STRING",
          station_id:      "STRING",
          station_name:    "STRING",
          target_time:     "STRING",
          prediction_time: "STRING",
          horizon_minutes: "INT64",
          threshold:       "FLOAT64",
        },
      });
    }

    if (email) {
      await sendConfirmationEmail(email, stationName, stationId, targetTime, predictionTime);
    }

    return NextResponse.json({ ok: true, count: cleanHorizons.length });
  } catch (err) {
    console.error("Subscribe API error:", err);
    return NextResponse.json({ error: "Could not save your subscription. Try again." }, { status: 500 });
  }
}
