import { NextRequest, NextResponse } from "next/server";
import snowflake from "snowflake-sdk";
import fs from "fs";
import path from "path";
import { createPrivateKey } from "crypto";

// Ride Explorer 3D map data source. Was querying BigQuery's raw
// station_hourly_flow table directly, but BigQuery's no-billing sandbox mode
// auto-expires every table in the project after 60 days (a dataset-level
// default, not something either sync script configured) and can't be
// disabled without linking a billing account. Moved to Snowflake instead —
// already funded via the existing trial, so this adds no new billing risk.
// Reads the pre-aggregated ride_explorer_profile table (built by
// data_ingestion/build_ride_explorer_profile.py, pushed by
// data_ingestion/sync_ride_explorer_to_snowflake.py) instead of raw trip rows.

const METRIC_COL: Record<string, string> = {
  all_all: "TOTAL_DEPARTURES",
  ebike_all: "TOTAL_EBIKE_TRIPS",
  classic_all: "TOTAL_CLASSIC_TRIPS",
  all_member: "TOTAL_MEMBER_TRIPS",
  all_casual: "TOTAL_CASUAL_TRIPS",
  ebike_member: "TOTAL_EBIKE_TRIPS",
  ebike_casual: "TOTAL_EBIKE_TRIPS",
  classic_member: "TOTAL_CLASSIC_TRIPS",
  classic_casual: "TOTAL_CLASSIC_TRIPS",
};

const VALID_BOROUGHS = new Set([
  "Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island", "Jersey City", "Hoboken",
]);

type SnowflakeRow = {
  STATION_ID: string;
  STATION_NAME: string;
  LAT: number;
  LON: number;
  CAPACITY: number;
  BOROUGH: string;
  TOTAL_RIDES: number;
  AVG_RIDES: number;
};

function getPrivateKey(): string {
  let pem: string;
  if (process.env.SNOWFLAKE_PRIVATE_KEY) {
    pem = process.env.SNOWFLAKE_PRIVATE_KEY.replace(/\\n/g, "\n");
  } else {
    const keyPath = path.resolve(process.cwd(), "..", "data_ingestion", "snowflake_key.p8");
    pem = fs.readFileSync(keyPath, "utf8");
  }
  return createPrivateKey({ key: pem, format: "pem" })
    .export({ type: "pkcs8", format: "pem" })
    .toString();
}

function querySnowflake(sql: string, binds: unknown[]): Promise<SnowflakeRow[]> {
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
        binds: binds as snowflake.Bind[],
        complete: (execErr, _stmt, rows) => {
          conn.destroy(() => {});
          if (execErr) reject(execErr);
          else resolve((rows ?? []) as SnowflakeRow[]);
        },
      });
    });
  });
}

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;

  const year = parseInt(sp.get("year") ?? "2026", 10);
  const month = sp.get("month") !== "all" && sp.has("month") ? parseInt(sp.get("month")!, 10) : null;
  const dow = sp.get("dow") !== "all" && sp.has("dow") ? parseInt(sp.get("dow")!, 10) : null;
  const hour = sp.get("hour") !== "all" && sp.has("hour") ? parseInt(sp.get("hour")!, 10) : null;
  const bikeType = sp.get("bike_type") ?? "all";
  const riderType = sp.get("rider_type") ?? "all";

  const boroughParam = sp.get("boroughs") ?? "";
  const boroughs = boroughParam
    ? boroughParam.split(",").map((b) => b.trim()).filter((b) => VALID_BOROUGHS.has(b))
    : [];

  const metricCol = METRIC_COL[`${bikeType}_${riderType}`] ?? "TOTAL_DEPARTURES";

  const where: string[] = ["year = ?", "lat IS NOT NULL", "lon IS NOT NULL"];
  const binds: unknown[] = [year];

  if (month !== null) { where.push("month = ?"); binds.push(month); }
  if (dow !== null) { where.push("day_of_week = ?"); binds.push(dow); }
  if (hour !== null) { where.push("hour_et = ?"); binds.push(hour); }
  if (boroughs.length > 0 && boroughs.length < VALID_BOROUGHS.size) {
    where.push(`borough IN (${boroughs.map(() => "?").join(",")})`);
    binds.push(...boroughs);
  }

  // hours_sampled weights each pre-aggregated row so avg_rides stays a true
  // per-hour average instead of averaging already-summed buckets.
  const sql = `
    SELECT
      station_id,
      ANY_VALUE(station_name) AS station_name,
      ANY_VALUE(lat)          AS lat,
      ANY_VALUE(lon)          AS lon,
      ANY_VALUE(capacity)     AS capacity,
      ANY_VALUE(borough)      AS borough,
      SUM(${metricCol})       AS total_rides,
      SUM(${metricCol}) / NULLIF(SUM(hours_sampled), 0) AS avg_rides
    FROM ride_explorer_profile
    WHERE ${where.join(" AND ")}
    GROUP BY station_id
    HAVING SUM(${metricCol}) > 0
    ORDER BY total_rides DESC
  `;

  try {
    const rows = await querySnowflake(sql, binds);

    const data = rows.map((r) => ({
      station_id: String(r.STATION_ID ?? ""),
      station_name: String(r.STATION_NAME ?? "Unknown"),
      lat: Number(r.LAT ?? 0),
      lon: Number(r.LON ?? 0),
      capacity: Number(r.CAPACITY ?? 0),
      borough: String(r.BOROUGH ?? "Unknown"),
      total_rides: Number(r.TOTAL_RIDES ?? 0),
      avg_rides: Number(r.AVG_RIDES ?? 0),
    }));

    return NextResponse.json(data, {
      headers: { "Cache-Control": "public, s-maxage=86400, stale-while-revalidate=3600" },
    });
  } catch (err) {
    console.error("Ride Explorer (Snowflake) error:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 500 }
    );
  }
}
