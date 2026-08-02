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
  let body: { email?: unknown; station_id?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request" }, { status: 400 });
  }

  if (!body.email || typeof body.email !== "string") {
    return NextResponse.json({ error: "Provide your email address." }, { status: 400 });
  }

  const email     = body.email.trim();
  const stationId = typeof body.station_id === "string" && body.station_id.trim()
    ? body.station_id.trim()
    : null;

  if (!email) {
    return NextResponse.json({ error: "Email is required." }, { status: 400 });
  }

  const bq    = getBQ();
  const subs  = `\`${PROJECT}.${DATASET}.subscribers\``;
  const unsub = `\`${PROJECT}.${DATASET}.unsubscribed\``;

  try {
    // Find subscriptions — either for one station or all stations
    const [rows] = await bq.query(stationId ? {
      query: `SELECT email, station_id, station_name, target_time, created_at
              FROM ${subs}
              WHERE email = @email AND station_id = @station_id`,
      params: { email, station_id: stationId },
    } : {
      query: `SELECT email, station_id, station_name, target_time, created_at
              FROM ${subs}
              WHERE email = @email`,
      params: { email },
    });

    if (rows.length === 0) {
      return NextResponse.json({ ok: true, found: false });
    }

    // Archive each unique (email, station_id) to unsubscribed
    const seen = new Set<string>();
    for (const row of rows) {
      const sid = String(row.station_id ?? "");
      if (seen.has(sid)) continue;
      seen.add(sid);

      await bq.query({
        query: `INSERT INTO ${unsub} (email, station_id, station_name, target_time, subscribed_at, unsubscribed_at)
                VALUES (@email, @station_id, @station_name, @target_time, @subscribed_at, CURRENT_TIMESTAMP())`,
        params: {
          email,
          station_id:    sid,
          station_name:  row.station_name != null ? String(row.station_name) : null,
          target_time:   row.target_time  != null ? String(row.target_time)  : null,
          subscribed_at: row.created_at?.value ?? row.created_at ?? null,
        },
        types: {
          email:         "STRING",
          station_id:    "STRING",
          station_name:  "STRING",
          target_time:   "STRING",
          subscribed_at: "TIMESTAMP",
        },
      });
    }

    // Delete matching rows
    await bq.query(stationId ? {
      query: `DELETE FROM ${subs} WHERE email = @email AND station_id = @station_id`,
      params: { email, station_id: stationId },
    } : {
      query: `DELETE FROM ${subs} WHERE email = @email`,
      params: { email },
    });

    return NextResponse.json({ ok: true, found: true, station_id: stationId });
  } catch (err) {
    console.error("Unsubscribe error:", err);
    return NextResponse.json(
      { error: "Could not process your request. Try again." },
      { status: 500 }
    );
  }
}
