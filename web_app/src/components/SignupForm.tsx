"use client";

import { useEffect, useMemo, useState } from "react";

type TimeSlot = { value: string; label: string };

function buildTargetTimeSlots(): TimeSlot[] {
  const slots: TimeSlot[] = [];
  for (let h = 0; h < 24; h++) {
    for (let m = 0; m < 60; m += 30) {
      const value = `${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}`;
      const period = h >= 12 ? "PM" : "AM";
      const displayH = h % 12 === 0 ? 12 : h % 12;
      const label = `${displayH}:${m.toString().padStart(2, "0")} ${period}`;
      slots.push({ value, label });
    }
  }
  return slots;
}

const TARGET_TIME_SLOTS = buildTargetTimeSlots();

type StationOption = { station_id: string; station_name: string };

const SELECT_CLASS =
  "w-full rounded-lg border border-black/15 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-blue-500 dark:border-white/20 dark:bg-zinc-900 dark:text-white";

const INPUT_CLASS =
  "w-full rounded-lg border border-black/15 bg-transparent px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-white/20";

export default function SignupForm({ initialStationId }: { initialStationId: string }) {
  const [stations, setStations] = useState<StationOption[]>([]);
  const [stationsError, setStationsError] = useState(false);

  // Signup form state
  const [email, setEmail] = useState("");
  const [stationId, setStationId] = useState(initialStationId);
  const [targetTime, setTargetTime] = useState("");
  const [status, setStatus] = useState<"idle" | "submitting" | "done">("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Unsubscribe form state
  const [unsubEmail, setUnsubEmail] = useState("");
  const [unsubStatus, setUnsubStatus] = useState<"idle" | "submitting" | "done" | "error">("idle");
  const [unsubError, setUnsubError] = useState("");

  useEffect(() => {
    fetch("/api/stations")
      .then((r) => {
        if (!r.ok) throw new Error(`API ${r.status}`);
        return r.json();
      })
      .then((data: { station_id: string; station_name: string }[]) => {
        const opts = data
          .map((s) => ({ station_id: s.station_id, station_name: s.station_name }))
          .sort((a, b) => a.station_name.localeCompare(b.station_name));
        setStations(opts);
      })
      .catch(() => setStationsError(true));
  }, []);

  const selectedStationName = useMemo(
    () => stations.find((s) => s.station_id === stationId)?.station_name,
    [stations, stationId]
  );

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErrorMsg(null);
    if (!email.trim()) {
      setErrorMsg("Enter your email address.");
      return;
    }
    if (!stationId) {
      setErrorMsg("Choose a station.");
      return;
    }
    setStatus("submitting");
    try {
      const res = await fetch("/api/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim(),
          station_id: stationId,
          target_time: targetTime || null,
          horizons: [60, 180, 360, 720, 1440, 2880],
          threshold: 1,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setErrorMsg(data.error ?? "Something went wrong.");
        setStatus("idle");
        return;
      }
      setStatus("done");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).fbq?.("track", "Lead");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).gtag?.("event", "signup_complete", { station_id: stationId });
    } catch {
      setErrorMsg("Network error. Try again.");
      setStatus("idle");
    }
  }

  async function handleUnsubscribe(e: React.FormEvent) {
    e.preventDefault();
    setUnsubError("");
    if (!unsubEmail.trim()) {
      setUnsubError("Enter your email address.");
      return;
    }
    setUnsubStatus("submitting");
    try {
      const res = await fetch("/api/unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: unsubEmail.trim() }),
      });
      const data = await res.json();
      if (res.ok) {
        setUnsubStatus("done");
      } else {
        setUnsubStatus("error");
        setUnsubError(data.error ?? "Something went wrong.");
      }
    } catch {
      setUnsubStatus("error");
      setUnsubError("Network error. Try again.");
    }
  }

  return (
    <div className="w-full max-w-md space-y-6">
      {/* --- Sign-up card --- */}
      {status === "done" ? (
        <div className="rounded-xl border border-black/10 p-8 text-center dark:border-white/15">
          <div className="mb-2 text-2xl">&#10003;</div>
          <h2 className="mb-2 text-xl font-semibold">You&apos;re signed up</h2>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            We&apos;ll alert you about bike availability
            {selectedStationName ? ` at ${selectedStationName}` : ""}
            {targetTime
              ? ` around ${TARGET_TIME_SLOTS.find((s) => s.value === targetTime)?.label}`
              : ""}
            .
          </p>
          <a
            href="/"
            className="mt-6 inline-block rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            Back to the map
          </a>
        </div>
      ) : (
        <form
          onSubmit={handleSubmit}
          className="rounded-xl border border-black/10 p-8 space-y-6 dark:border-white/15"
        >
          <div>
            <label className="mb-1 block text-sm font-medium" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              className={INPUT_CLASS}
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium" htmlFor="station">
              Station
            </label>
            {stationsError ? (
              <input
                id="station"
                type="text"
                value={stationId}
                onChange={(e) => setStationId(e.target.value)}
                placeholder="Station ID"
                className={INPUT_CLASS}
              />
            ) : (
              <select
                id="station"
                value={stationId}
                onChange={(e) => setStationId(e.target.value)}
                className={SELECT_CLASS}
              >
                <option value="">
                  {stations.length === 0 ? "Loading stations..." : "Select a station"}
                </option>
                {stations.map((s) => (
                  <option key={s.station_id} value={s.station_id}>
                    {s.station_name}
                  </option>
                ))}
              </select>
            )}
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium" htmlFor="target-time">
              When do you want the station alert?{" "}
              <span className="text-zinc-500">(optional)</span>
            </label>
            <select
              id="target-time"
              value={targetTime}
              onChange={(e) => setTargetTime(e.target.value)}
              className={SELECT_CLASS}
            >
              <option value="">Select a time</option>
              {TARGET_TIME_SLOTS.map((slot) => (
                <option key={slot.value} value={slot.value}>
                  {slot.label}
                </option>
              ))}
            </select>
          </div>

          {errorMsg && (
            <p className="rounded-lg bg-red-100 px-3 py-2 text-sm text-red-700 dark:bg-red-950/50 dark:text-red-300">
              {errorMsg}
            </p>
          )}

          <button
            type="submit"
            disabled={status === "submitting"}
            className="w-full rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-60"
          >
            {status === "submitting" ? "Signing up..." : "Get alerts"}
          </button>
        </form>
      )}

      {/* --- Unsubscribe card --- */}
      <div className="rounded-xl border border-black/10 p-6 dark:border-white/15">
        <h2 className="mb-1 text-base font-semibold">Unsubscribe from alerts</h2>
        <p className="mb-4 text-sm text-zinc-500 dark:text-zinc-400">
          Enter the email you signed up with to stop all alerts.
        </p>
        {unsubStatus === "done" ? (
          <p className="text-sm text-green-700 dark:text-green-400">
            &#10003; Done. You won&apos;t receive any more alerts.
          </p>
        ) : (
          <form onSubmit={handleUnsubscribe} className="space-y-3">
            <input
              type="email"
              value={unsubEmail}
              onChange={(e) => setUnsubEmail(e.target.value)}
              placeholder="you@example.com"
              className={INPUT_CLASS}
            />
            {unsubError && (
              <p className="text-sm text-red-600 dark:text-red-400">{unsubError}</p>
            )}
            <button
              type="submit"
              disabled={unsubStatus === "submitting"}
              className="w-full rounded-lg border border-black/15 px-4 py-2 text-sm font-medium hover:bg-zinc-100 disabled:opacity-60 dark:border-white/20 dark:hover:bg-zinc-800"
            >
              {unsubStatus === "submitting" ? "Unsubscribing..." : "Unsubscribe"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
