import { NextResponse } from "next/server";
import snowflake from "snowflake-sdk";
import fs from "fs";
import path from "path";
import { createPrivateKey } from "crypto";

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

function getConnection() {
  return snowflake.createConnection({
    account: process.env.SNOWFLAKE_ACCOUNT!,
    username: process.env.SNOWFLAKE_USER!,
    authenticator: "SNOWFLAKE_JWT",
    privateKey: getPrivateKey(),
    database: process.env.SNOWFLAKE_DATABASE ?? "CITIBIKE",
    schema: process.env.SNOWFLAKE_SCHEMA ?? "PUBLIC",
    warehouse: process.env.SNOWFLAKE_WAREHOUSE ?? "COMPUTE_WH",
  });
}

function querySnowflake(
  sql: string,
  binds: unknown[]
): Promise<Record<string, unknown>[]> {
  return new Promise((resolve, reject) => {
    const conn = getConnection();
    conn.connect((err) => {
      if (err) { reject(err); return; }
      conn.execute({
        sqlText: sql,
        binds: binds as snowflake.Binds,
        complete: (execErr, _stmt, rows) => {
          conn.destroy(() => {});
          if (execErr) reject(execErr);
          else resolve((rows ?? []) as Record<string, unknown>[]);
        },
      });
    });
  });
}

function executeSnowflake(sql: string, binds: unknown[]): Promise<void> {
  return new Promise((resolve, reject) => {
    const conn = getConnection();
    conn.connect((err) => {
      if (err) { reject(err); return; }
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
  let body: { id?: unknown; email?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request" }, { status: 400 });
  }

  try {
    if (body.email && typeof body.email === "string") {
      // Email-based unsubscribe (from the Get Alerts page form)
      const email = body.email.trim();
      if (!email) {
        return NextResponse.json({ error: "Email is required." }, { status: 400 });
      }

      // Look up all subscriber rows for this email
      const rows = await querySnowflake(
        "SELECT id, email, station_id, target_time, created_at FROM subscribers WHERE email = ?",
        [email]
      );

      if (rows.length === 0) {
        // No subscription found — still return ok (idempotent)
        return NextResponse.json({ ok: true, found: false });
      }

      // Archive each row to unsubscribed before deleting
      for (const row of rows) {
        await executeSnowflake(
          "INSERT INTO unsubscribed (email, station_id, target_time, subscribed_at) VALUES (?, ?, ?, ?)",
          [row.email, row.station_id, row.target_time ?? null, row.created_at]
        );
      }

      // Delete all rows for this email
      await executeSnowflake("DELETE FROM subscribers WHERE email = ?", [email]);

      return NextResponse.json({ ok: true, found: true });

    } else if (body.id !== undefined) {
      // ID-based unsubscribe (legacy — from old email footer links)
      const id = typeof body.id === "number" ? body.id : parseInt(String(body.id));
      if (!Number.isFinite(id) || id <= 0) {
        return NextResponse.json({ error: "Invalid subscriber ID" }, { status: 400 });
      }

      const rows = await querySnowflake(
        "SELECT email, station_id, target_time, created_at FROM subscribers WHERE id = ?",
        [id]
      );

      if (rows.length === 0) {
        return NextResponse.json({ ok: true });
      }

      const { email, station_id, target_time, created_at } = rows[0];

      await executeSnowflake(
        "INSERT INTO unsubscribed (email, station_id, target_time, subscribed_at) VALUES (?, ?, ?, ?)",
        [email, station_id, target_time ?? null, created_at]
      );

      await executeSnowflake("DELETE FROM subscribers WHERE email = ?", [email]);

      return NextResponse.json({ ok: true });

    } else {
      return NextResponse.json(
        { error: "Provide either email or id." },
        { status: 400 }
      );
    }
  } catch (err) {
    console.error("Unsubscribe error:", err);
    return NextResponse.json(
      { error: "Could not process your request. Try again." },
      { status: 500 }
    );
  }
}
