import { NextRequest, NextResponse } from "next/server";
import { BigQuery } from "@google-cloud/bigquery";
import path from "path";

// Ride Explorer 3D map data source. Originally tried BigQuery sandbox (expired
// tables) and Snowflake (trial). Now on BigQuery with billing enabled —
// reads the pre-aggregated ride_explorer_profile table built by
// data_ingestion/build_ride_explorer_profile.py and synced by
// data_ingestion/sync_ride_explorer_to_bigquery.py.

const PROJECT = process.env.GCP_PROJECT_ID ?? "citibike-tableau-501513";
const DATASET  = process.env.GCP_DATASET    ?? "citibike";

// BigQuery column names are lowercase (match the table DDL).
const METRIC_COL: Record<string, string> = {
  all_all:       "total_departures",
  ebike_all:     "total_ebike_trips",
  classic_all:   "total_classic_trips",
  all_member:    "total_member_trips",
  all_casual:    "total_casual_trips",
  ebike_member:  "total_ebike_trips",
  ebike_casual:  "total_ebike_trips",
  classic_member: "total_classic_trips",
  classic_casual: "total_classic_trips",
};

const VALID_BOROUGHS = new Set([
  "Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island", "Jersey City", "Hoboken",
]);

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

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;

  const year      = parseInt(sp.get("year") ?? "2026", 10);
  const month     = sp.get("month") !== "all" && sp.has("month") ? parseInt(sp.get("month")!, 10) : null;
  const dow       = sp.get("dow")   !== "all" && sp.has("dow")   ? parseInt(sp.get("dow")!,   10) : null;
  const hour      = sp.get("hour")  !== "all" && sp.has("hour")  ? parseInt(sp.get("hour")!,  10) : null;
  const bikeType  = sp.get("bike_type")  ?? "all";
  const riderType = sp.get("rider_type") ?? "all";

  const boroughParam = sp.get("boroughs") ?? "";
  // Validate each borough against the allowlist before interpolating into SQL.
  const boroughs = boroughParam
    ? boroughParam.split(",").map((b) => b.trim()).filter((b) => VALID_BOROUGHS.has(b))
    : [];

  const metricCol = METRIC_COL[`${bikeType}_${riderType}`] ?? "total_departures";

  // Use negative sentinels for optional numeric params so BigQuery named params
  // stay statically typed (INT64). (-1 is never a valid month/dow/hour.)
  let query = `
    SELECT
      station_id,
      ANY_VALUE(station_name) AS station_name,
      ANY_VALUE(lat)          AS lat,
      ANY_VALUE(lon)          AS lon,
      ANY_VALUE(capacity)     AS capacity,
      ANY_VALUE(borough)      AS borough,
      SUM(${metricCol})       AS total_rides,
      SUM(${metricCol}) / NULLIF(SUM(hours_sampled), 0) AS avg_rides
    FROM \`${PROJECT}.${DATASET}.ride_explorer_profile\`
    WHERE year = @year
      AND lat IS NOT NULL
      AND lon IS NOT NULL
      AND (@month < 0 OR month = @month)
      AND (@dow   < 0 OR day_of_week = @dow)
      AND (@hour  < 0 OR hour_et = @hour)
  `;

  // Borough list is already validated against VALID_BOROUGHS — safe to embed as literals.
  if (boroughs.length > 0 && boroughs.length < VALID_BOROUGHS.size) {
    query += `\n      AND borough IN (${boroughs.map((b) => `'${b.replace(/'/g, "''")}'`).join(", ")})`;
  }

  query += `
    GROUP BY station_id
    HAVING SUM(${metricCol}) > 0
    ORDER BY total_rides DESC
  `;

  const bq = getBQ();

  try {
    const [rows] = await bq.query({
      query,
      params: {
        year,
        month: month ?? -1,
        dow:   dow   ?? -1,
        hour:  hour  ?? -1,
      },
    });

    const data = rows.map((r) => ({
      station_id:   String(r.station_id   ?? ""),
      station_name: String(r.station_name ?? "Unknown"),
      lat:          Number(r.lat          ?? 0),
      lon:          Number(r.lon          ?? 0),
      capacity:     Number(r.capacity     ?? 0),
      borough:      String(r.borough      ?? "Unknown"),
      total_rides:  Number(r.total_rides  ?? 0),
      avg_rides:    Number(r.avg_rides    ?? 0),
    }));

    return NextResponse.json(data, {
      headers: { "Cache-Control": "public, s-maxage=86400, stale-while-revalidate=3600" },
    });
  } catch (err) {
    console.error("Ride Explorer (BigQuery) error:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 500 }
    );
  }
}
