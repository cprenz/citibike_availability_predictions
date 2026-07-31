import { NextResponse } from "next/server";
import snowflake from "snowflake-sdk";
import fs from "fs";
import path from "path";
import { createPrivateKey } from "crypto";

function formatTime(hhmm: string): string {
  const [hStr, mStr] = hhmm.split(":");
  const h = parseInt(hStr, 10);
  const m = mStr ?? "00";
  const period = h >= 12 ? "PM" : "AM";
  const displayH = h % 12 === 0 ? 12 : h % 12;
  return `${displayH}:${m} ${period}`;
}

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

function getPrivateKey(): string {
  let pem: string;
  if (process.env.SNOWFLAKE_PRIVATE_KEY) {
    pem = process.env.SNOWFLAKE_PRIVATE_KEY.replace(/\\n/g, "\n");
  } else {
    const keyPath = path.resolve(
      process.cwd(),
      "..",
      "data_ingestion",
      "snowflake_key.p8"
    );
    pem = fs.readFileSync(keyPath, "utf8");
  }
  return createPrivateKey({ key: pem, format: "pem" })
    .export({ type: "pkcs8", format: "pem" })
    .toString();
}

function executeSnowflake(sql: string, binds: unknown[]): Promise<void> {
  return new Promise((resolve, reject) => {
    const conn = snowflake.createConnection({
      account: process.env.SNOWFLAKE_ACCOUNT!,
      username: process.env.SNOWFLAKE_USER!,
      authenticator: "SNOWFLAKE_JWT",
      privateKey: getPrivateKey(),
      database: process.env.SNOWFLAKE_DATABASE ?? "CITIBIKE",
      schema: process.env.SNOWFLAKE_SCHEMA ?? "PUBLIC",
      warehouse: process.env.SNOWFLAKE_WAREHOUSE ?? "COMPUTE_WH",
    });

    conn.connect((connectErr) => {
      if (connectErr) {
        reject(connectErr);
        return;
      }
      conn.execute({
        sqlText: sql,
        binds: binds as snowflake.Binds,
        complete: (execErr) => {
          conn.destroy(() => {});
          if (execErr) reject(execErr);
          else resolve();
        },
      });
    });
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
  const alertLabel = targetTime ? formatTime(targetTime) : null;
  const predLabel = predictionTime ? formatTime(predictionTime) : null;
  const stationUrl = `https://bikepredict.fyi/station/${stationId}`;
  const unsubUrl = `https://bikepredict.fyi/signup`;

  const html = `<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:system-ui,-apple-system,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 16px">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%">

        <!-- Header -->
        <tr>
          <td style="background:#1e40af;border-radius:10px 10px 0 0;padding:24px 32px">
            <span style="color:#fff;font-size:20px;font-weight:700;letter-spacing:-0.3px">BikePredict</span>
            <span style="color:#93c5fd;font-size:13px;margin-left:12px">bikepredict.fyi</span>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="background:#fff;padding:32px">
            <p style="margin:0 0 6px;font-size:22px;font-weight:700;color:#111;line-height:1.2">
              You&rsquo;re signed up for alerts
            </p>
            <p style="margin:0 0 24px;font-size:14px;color:#6b7280">
              Here&rsquo;s when you&rsquo;ll hear from us and what each email will tell you.
            </p>

            <!-- Station info box -->
            <table width="100%" cellpadding="0" cellspacing="0"
              style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:24px">
              <tr>
                <td style="padding:20px 24px">
                  <div style="font-size:11px;font-weight:600;color:#6b7280;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:4px">Station</div>
                  <div style="font-size:16px;font-weight:700;color:#111">${stationLabel}</div>
                  ${alertLabel ? `
                  <div style="margin-top:14px;padding-top:14px;border-top:1px solid #e5e7eb">
                    <div style="font-size:11px;font-weight:600;color:#6b7280;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:4px">You&rsquo;ll get an email at</div>
                    <div style="font-size:16px;font-weight:700;color:#111">${alertLabel} daily</div>
                  </div>` : ""}
                  ${predLabel ? `
                  <div style="margin-top:14px;padding-top:14px;border-top:1px solid #e5e7eb">
                    <div style="font-size:11px;font-weight:600;color:#6b7280;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:4px">Predictions for</div>
                    <div style="font-size:16px;font-weight:700;color:#111">${predLabel}</div>
                  </div>` : ""}
                </td>
              </tr>
            </table>

            <p style="margin:0 0 24px;font-size:14px;color:#374151;line-height:1.5">
              ${alertLabel && predLabel
                ? `Your alert arrives at <strong>${alertLabel}</strong> each day with predicted bike availability for <strong>${predLabel}</strong> &mdash; so you can plan before you need to leave.`
                : alertLabel
                  ? `Your alert arrives at <strong>${alertLabel}</strong> each day with the latest bike availability predictions for this station.`
                  : `You&rsquo;ll receive a daily email with the latest bike availability predictions for this station.`
              }
              You&rsquo;ll get one email per day per station. Visit the station page to see the full forecast across all time horizons.
            </p>

            <!-- CTA button -->
            <table cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:#2563eb;border-radius:7px">
                  <a href="${stationUrl}"
                     style="display:inline-block;padding:12px 24px;color:#fff;font-size:14px;font-weight:600;text-decoration:none">
                    See predictions for this station &rarr;
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Footer -->
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
      headers: {
        "Authorization": `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
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

  const email = body.email?.trim() || null;
  const phone = body.phone?.trim() || null;
  const stationId = body.station_id?.trim();
  const stationName = body.station_name?.trim() || null;
  const targetTime = body.target_time?.trim() || null;
  const predictionTime = body.prediction_time?.trim() || null;
  const horizons = Array.isArray(body.horizons) ? body.horizons : [];
  const threshold =
    typeof body.threshold === "number" && Number.isFinite(body.threshold)
      ? body.threshold
      : null;

  if (!email && !phone) {
    return NextResponse.json(
      { error: "Provide at least an email or a phone number." },
      { status: 400 }
    );
  }
  if (email && !isValidEmail(email)) {
    return NextResponse.json(
      { error: "That email address doesn't look valid." },
      { status: 400 }
    );
  }
  if (!stationId) {
    return NextResponse.json(
      { error: "Please choose a station." },
      { status: 400 }
    );
  }
  if (targetTime && !isValidTime(targetTime)) {
    return NextResponse.json(
      { error: "Target time must be in HH:MM format." },
      { status: 400 }
    );
  }
  if (predictionTime && !isValidTime(predictionTime)) {
    return NextResponse.json(
      { error: "Prediction time must be in HH:MM format." },
      { status: 400 }
    );
  }
  const cleanHorizons = horizons.filter((h) => VALID_HORIZONS.has(h));
  if (cleanHorizons.length === 0) {
    return NextResponse.json(
      { error: "Select at least one alert horizon." },
      { status: 400 }
    );
  }

  // Build a single multi-row INSERT — avoids needing explicit transactions.
  const placeholders = cleanHorizons.map(() => "(?, ?, ?, ?, ?, ?, ?, ?)").join(", ");
  const binds = cleanHorizons.flatMap((h) => [
    email,
    phone,
    stationId,
    stationName,
    targetTime,
    predictionTime,
    h,
    threshold,
  ]);

  try {
    await executeSnowflake(
      `INSERT INTO subscribers (email, phone, station_id, station_name, target_time, prediction_time, horizon_minutes, threshold) VALUES ${placeholders}`,
      binds
    );

    // Send confirmation email — must be awaited; Vercel kills the invocation
    // the moment the response is returned, so fire-and-forget doesn't work.
    if (email) {
      await sendConfirmationEmail(email, stationName, stationId, targetTime, predictionTime);
    }

    return NextResponse.json({ ok: true, count: cleanHorizons.length });
  } catch (err) {
    console.error("Subscribe API error:", err);
    return NextResponse.json(
      { error: "Could not save your subscription. Try again." },
      { status: 500 }
    );
  }
}
