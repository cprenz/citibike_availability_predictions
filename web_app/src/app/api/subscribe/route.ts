import { NextResponse } from "next/server";
import snowflake from "snowflake-sdk";
import fs from "fs";
import path from "path";
import { createPrivateKey } from "crypto";

const VALID_HORIZONS = new Set([60, 180, 360, 720, 1440, 2880]);

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

  // Build a single multi-row INSERT — avoids needing explicit transactions.
  const placeholders = cleanHorizons.map(() => "(?, ?, ?, ?, ?)").join(", ");
  const binds = cleanHorizons.flatMap((h) => [
    email,
    phone,
    stationId,
    h,
    threshold,
  ]);

  try {
    await executeSnowflake(
      `INSERT INTO subscribers (email, phone, station_id, horizon_minutes, threshold) VALUES ${placeholders}`,
      binds
    );
    return NextResponse.json({ ok: true, count: cleanHorizons.length });
  } catch (err) {
    console.error("Subscribe API error:", err);
    return NextResponse.json(
      { error: "Could not save your subscription. Try again." },
      { status: 500 }
    );
  }
}
