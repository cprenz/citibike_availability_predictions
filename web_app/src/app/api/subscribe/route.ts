import { NextResponse } from "next/server";
import { Pool } from "pg";

const pool = new Pool({
  host: process.env.PGHOST ?? "localhost",
  port: parseInt(process.env.PGPORT ?? "5555"),
  database: process.env.PGDATABASE ?? "citibike",
  user: process.env.PGUSER ?? "citibike_admin",
  password: process.env.PGPASSWORD ?? "password",
});

const VALID_HORIZONS = new Set([60, 180, 360, 720, 1440, 10080]);

type SubscribeBody = {
  email?: string | null;
  phone?: string | null;
  station_id?: string;
  horizons?: number[];
  threshold?: number | null;
};

function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
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
  const horizons = Array.isArray(body.horizons) ? body.horizons : [];
  const threshold =
    typeof body.threshold === "number" && Number.isFinite(body.threshold)
      ? body.threshold
      : null;

  // Validation — mirrors the subscribers_contact_check DB constraint plus app rules
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
  const cleanHorizons = horizons.filter((h) => VALID_HORIZONS.has(h));
  if (cleanHorizons.length === 0) {
    return NextResponse.json(
      { error: "Select at least one alert horizon." },
      { status: 400 }
    );
  }

  const client = await pool.connect();
  try {
    // One row per (contact, station, horizon) — matches the subscribers grain.
    await client.query("BEGIN");
    for (const horizon of cleanHorizons) {
      await client.query(
        `INSERT INTO subscribers (email, phone, station_id, horizon_minutes, threshold)
         VALUES ($1, $2, $3, $4, $5)`,
        [email, phone, stationId, horizon, threshold]
      );
    }
    await client.query("COMMIT");
    return NextResponse.json({ ok: true, count: cleanHorizons.length });
  } catch (err) {
    await client.query("ROLLBACK");
    console.error("Subscribe API error:", err);
    return NextResponse.json(
      { error: "Could not save your subscription. Try again." },
      { status: 500 }
    );
  } finally {
    client.release();
  }
}
