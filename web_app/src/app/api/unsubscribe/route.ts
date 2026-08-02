import { NextResponse } from "next/server";
import { BigQuery } from "@google-cloud/bigquery";
import path from "path";

const PROJECT = process.env.GCP_PROJECT_ID ?? "citibike-tableau-501513";
const DATASET  = process.env.GCP_DATASET    ?? "citibike";

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

export async function POST(request: Request) {
  let body: { email?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request" }, { status: 400 });
  }

  if (!body.email || typeof body.email !== "string") {
    return NextResponse.json({ error: "Provide your email address." }, { status: 400 });
  }

  const email = body.email.trim();
  if (!email) {
    return NextResponse.json({ error: "Email is required." }, { status: 400 });
  }

  const bq    = getBQ();
  const subs  = `\`${PROJECT}.${DATASET}.subscribers\``;
  const unsub = `\`${PROJECT}.${DATASET}.unsubscribed\``;

  try {
    // Find all subscriptions for this email
    const [rows] = await bq.query({
      query: `SELECT email, station_id, station_name, target_time, created_at
              FROM ${subs}
              WHERE email = @email`,
      params: { email },
    });

    if (rows.length === 0) {
      // Idempotent — no subscription found is still a success
      return NextResponse.json({ ok: true, found: false });
    }

    // Archive each unique (email, station_id) to unsubscribed, then delete
    const seen = new Set<string>();
    for (const row of rows) {
      const stationId = String(row.station_id ?? "");
      if (seen.has(stationId)) continue;
      seen.add(stationId);

      await bq.query({
        query: `INSERT INTO ${unsub} (email, station_id, station_name, target_time, subscribed_at, unsubscribed_at)
                VALUES (@email, @station_id, @station_name, @target_time, @subscribed_at, CURRENT_TIMESTAMP())`,
        params: {
          email,
          station_id:   stationId,
          station_name: row.station_name != null ? String(row.station_name) : null,
          target_time:  row.target_time  != null ? String(row.target_time)  : null,
          subscribed_at: row.created_at?.value ?? row.created_at ?? null,
        },
      });
    }

    // Delete all subscriber rows for this email in one shot
    await bq.query({
      query: `DELETE FROM ${subs} WHERE email = @email`,
      params: { email },
    });

    return NextResponse.json({ ok: true, found: true });
  } catch (err) {
    console.error("Unsubscribe error:", err);
    return NextResponse.json(
      { error: "Could not process your request. Try again." },
      { status: 500 }
    );
  }
}
