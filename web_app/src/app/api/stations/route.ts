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

export async function GET() {
  const bq = getBQ();
  const query = `
    WITH latest AS (
      SELECT station_id, MAX(predicted_at) AS max_at
      FROM \`${PROJECT}.${DATASET}.model_predictions\`
      GROUP BY station_id
    )
    SELECT
      mp.station_id,
      si.name                     AS station_name,
      si.lat,
      si.lon,
      si.capacity,
      mp.horizon_minutes,
      mp.predicted_prob_logistic,
      mp.predicted_value_lgbm,
      mp.predicted_value_linear,
      mp.pi_lower,
      mp.pi_upper,
      mp.predicted_at
    FROM \`${PROJECT}.${DATASET}.model_predictions\` mp
    JOIN latest l
      ON l.station_id = mp.station_id AND l.max_at = mp.predicted_at
    JOIN \`${PROJECT}.${DATASET}.station_information\` si
      ON si.station_id = mp.station_id
    WHERE si.lat IS NOT NULL AND si.lon IS NOT NULL
    ORDER BY mp.station_id, mp.horizon_minutes
  `;

  try {
    const [rows] = await bq.query({ query });

    // BigQuery returns lowercase column names matching the table DDL.
    const stationMap = new Map<string, {
      station_id: string;
      station_name: string;
      lat: number;
      lon: number;
      capacity: number;
      predicted_at: string;
      horizons: {
        horizon_minutes: number;
        predicted_prob_logistic: number;
        predicted_value_lgbm: number;
        predicted_value_linear: number;
        pi_lower: number;
        pi_upper: number;
      }[];
    }>();

    for (const row of rows) {
      const id = String(row.station_id);
      if (!stationMap.has(id)) {
        stationMap.set(id, {
          station_id: id,
          station_name: String(row.station_name ?? ""),
          lat: Number(row.lat),
          lon: Number(row.lon),
          capacity: Number(row.capacity ?? 0),
          // BigQuery TIMESTAMP fields come back as BigQuery.BigQueryTimestamp objects
          predicted_at: row.predicted_at?.value ?? String(row.predicted_at ?? ""),
          horizons: [],
        });
      }
      stationMap.get(id)!.horizons.push({
        horizon_minutes:         Number(row.horizon_minutes),
        predicted_prob_logistic: Number(row.predicted_prob_logistic ?? 0),
        predicted_value_lgbm:    Number(row.predicted_value_lgbm    ?? 0),
        predicted_value_linear:  Number(row.predicted_value_linear  ?? 0),
        pi_lower:                Number(row.pi_lower  ?? 0),
        pi_upper:                Number(row.pi_upper  ?? 0),
      });
    }

    return NextResponse.json(Array.from(stationMap.values()), {
      headers: { "Cache-Control": "public, s-maxage=300, stale-while-revalidate=60" },
    });
  } catch (err) {
    console.error("BigQuery /api/stations error:", err);
    return NextResponse.json({ error: "Failed to load station data." }, { status: 500 });
  }
}
