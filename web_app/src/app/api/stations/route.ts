import { NextResponse } from "next/server";
import snowflake from "snowflake-sdk";
import fs from "fs";
import path from "path";
import { createPrivateKey } from "crypto";

// Snowflake returns column names uppercase when tables were created without
// quoted identifiers (which write_pandas uses by default).
type SnowflakeRow = {
  STATION_ID: string;
  STATION_NAME: string;
  LAT: number;
  LON: number;
  CAPACITY: number;
  HORIZON_MINUTES: number;
  PREDICTED_PROB_LOGISTIC: number | null;
  PREDICTED_VALUE_LGBM: number | null;
  PI_LOWER: number | null;
  PI_UPPER: number | null;
};

const SQL = `
  WITH latest AS (
    SELECT MAX(predicted_at) AS ts FROM model_predictions
  )
  SELECT
    si.station_id,
    si.name                       AS station_name,
    si.lat,
    si.lon,
    si.capacity,
    mp.horizon_minutes,
    mp.predicted_prob_logistic,
    mp.predicted_value_lgbm,
    mp.pi_lower,
    mp.pi_upper
  FROM station_information si
  JOIN model_predictions mp ON si.station_id = mp.station_id
  CROSS JOIN latest
  WHERE mp.predicted_at = latest.ts
    AND si.lat  IS NOT NULL
    AND si.lon  IS NOT NULL
  ORDER BY si.station_id, mp.horizon_minutes
`;

function getPrivateKey(): string {
  let pem: string;
  // Vercel: PEM stored in env var (newlines may arrive as literal \n or real \n)
  if (process.env.SNOWFLAKE_PRIVATE_KEY) {
    pem = process.env.SNOWFLAKE_PRIVATE_KEY.replace(/\\n/g, "\n");
  } else {
    // Local: read from the .p8 file sitting two levels up from web_app/
    const keyPath = path.resolve(
      process.cwd(),
      "..",
      "data_ingestion",
      "snowflake_key.p8"
    );
    pem = fs.readFileSync(keyPath, "utf8");
  }
  // Round-trip through Node crypto to normalize PEM formatting — scrubs any
  // whitespace/line-ending quirks from Vercel env vars, then re-export as the
  // clean PEM string the Snowflake SDK expects for privateKey.
  return createPrivateKey({ key: pem, format: "pem" })
    .export({ type: "pkcs8", format: "pem" })
    .toString();
}

function querySnowflake(sql: string): Promise<SnowflakeRow[]> {
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
        complete: (execErr, _stmt, rows) => {
          conn.destroy(() => {});
          if (execErr) reject(execErr);
          else resolve((rows ?? []) as SnowflakeRow[]);
        },
      });
    });
  });
}

export async function GET() {
  try {
    const rows = await querySnowflake(SQL);

    const stationMap = new Map<
      string,
      {
        station_id: string;
        station_name: string;
        lat: number;
        lon: number;
        capacity: number;
        horizons: {
          horizon_minutes: number;
          predicted_prob_logistic: number;
          predicted_value_lgbm: number;
          pi_lower: number;
          pi_upper: number;
        }[];
      }
    >();

    for (const row of rows) {
      const id = row.STATION_ID;
      if (!stationMap.has(id)) {
        stationMap.set(id, {
          station_id: id,
          station_name: row.STATION_NAME,
          lat: Number(row.LAT),
          lon: Number(row.LON),
          capacity: Number(row.CAPACITY),
          horizons: [],
        });
      }
      stationMap.get(id)!.horizons.push({
        horizon_minutes: Number(row.HORIZON_MINUTES),
        predicted_prob_logistic: Number(row.PREDICTED_PROB_LOGISTIC ?? 0),
        predicted_value_lgbm: Number(row.PREDICTED_VALUE_LGBM ?? 0),
        pi_lower: Number(row.PI_LOWER ?? 0),
        pi_upper: Number(row.PI_UPPER ?? 0),
      });
    }

    return NextResponse.json(Array.from(stationMap.values()), {
      headers: {
        "Cache-Control": "public, s-maxage=300, stale-while-revalidate=60",
      },
    });
  } catch (err) {
    console.error("Stations API error:", err);
    return NextResponse.json(
      { error: "Failed to load station data" },
      { status: 500 }
    );
  }
}
