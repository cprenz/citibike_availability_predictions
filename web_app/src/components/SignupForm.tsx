"use client";

import { useEffect, useMemo, useState } from "react";

const HORIZONS = [
  { minutes: 60, label: "1 hour" },
  { minutes: 180, label: "3 hours" },
  { minutes: 360, label: "6 hours" },
  { minutes: 720, label: "12 hours" },
  { minutes: 1440, label: "24 hours" },
  { minutes: 10080, label: "Multi-day" },
];

type StationOption = { station_id: string; station_name: string };

export default function SignupForm({
  initialStationId,
}: {
  initialStationId: string;
}) {
  const [stations, setStations] = useState<StationOption[]>([]);
  const [stationsError, setStationsError] = useState(false);

  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [stationId, setStationId] = useState(initialStationId);
  const [horizons, setHorizons] = useState<number[]>([60, 180]);
  const [threshold, setThreshold] = useState(1);

  const [status, setStatus] = useState<"idle" | "submitting" | "done">("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Load the station list so the picker shows names, not raw UUIDs.
  useEffect(() => {
    fetch("/api/stations")
      .then((r) => {
        if (!r.ok) throw new Error(`API ${r.status}`);
        return r.json();
      })
      .then(
        (
          data: { station_id: string; station_name: string }[]
        ) => {
          const opts = data
            .map((s) => ({
              station_id: s.station_id,
              station_name: s.station_name,
            }))
            .sort((a, b) => a.station_name.localeCompare(b.station_name));
          setStations(opts);
        }
      )
      .catch(() => setStationsError(true));
  }, []);

  const selectedStationName = useMemo(
    () => stations.find((s) => s.station_id === stationId)?.station_name,
    [stations, stationId]
  );

  function toggleHorizon(minutes: number) {
    setHorizons((prev) =>
      prev.includes(minutes)
        ? prev.filter((m) => m !== minutes)
        : [...prev, minutes]
    );
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErrorMsg(null);

    if (!email.trim() && !phone.trim()) {
      setErrorMsg("Enter an email or a phone number.");
      return;
    }
    if (!stationId) {
      setErrorMsg("Choose a station.");
      return;
    }
    if (horizons.length === 0) {
      setErrorMsg("Pick at least one alert horizon.");
      return;
    }

    setStatus("submitting");
    try {
      const res = await fetch("/api/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim() || null,
          phone: phone.trim() || null,
          station_id: stationId,
          horizons,
          threshold,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setErrorMsg(data.error ?? "Something went wrong.");
        setStatus("idle");
        return;
      }
      setStatus("done");
    } catch {
      setErrorMsg("Network error. Try again.");
      setStatus("idle");
    }
  }

  if (status === "done") {
    return (
      <div className="w-full max-w-md rounded-xl border border-black/10 p-8 text-center dark:border-white/15">
        <div className="mb-2 text-2xl">✓</div>
        <h2 className="mb-2 text-xl font-semibold">You&apos;re signed up</h2>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          We&apos;ll alert you about bike availability
          {selectedStationName ? ` at ${selectedStationName}` : ""} at your
          selected horizons.
        </p>
        <a
          href="/"
          className="mt-6 inline-block rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          Back to the map
        </a>
      </div>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="w-full max-w-md space-y-6 rounded-xl border border-black/10 p-8 dark:border-white/15"
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
          className="w-full rounded-lg border border-black/15 bg-transparent px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-white/20"
        />
      </div>

      <div>
        <label className="mb-1 block text-sm font-medium" htmlFor="phone">
          Phone <span className="text-zinc-500">(optional)</span>
        </label>
        <input
          id="phone"
          type="tel"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          placeholder="+1 555 123 4567"
          className="w-full rounded-lg border border-black/15 bg-transparent px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-white/20"
        />
        <p className="mt-1 text-xs text-zinc-500">
          Provide at least one of email or phone.
        </p>
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
            className="w-full rounded-lg border border-black/15 bg-transparent px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-white/20"
          />
        ) : (
          <select
            id="station"
            value={stationId}
            onChange={(e) => setStationId(e.target.value)}
            className="w-full rounded-lg border border-black/15 bg-transparent px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-white/20"
          >
            <option value="">
              {stations.length === 0 ? "Loading stations…" : "Select a station"}
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
        <span className="mb-2 block text-sm font-medium">Alert me for</span>
        <div className="grid grid-cols-2 gap-2">
          {HORIZONS.map((h) => (
            <label
              key={h.minutes}
              className="flex items-center gap-2 rounded-lg border border-black/10 px-3 py-2 text-sm dark:border-white/15"
            >
              <input
                type="checkbox"
                checked={horizons.includes(h.minutes)}
                onChange={() => toggleHorizon(h.minutes)}
                className="accent-blue-600"
              />
              {h.label}
            </label>
          ))}
        </div>
      </div>

      <div>
        <label className="mb-1 block text-sm font-medium" htmlFor="threshold">
          Alert when predicted bikes available is at least
        </label>
        <input
          id="threshold"
          type="number"
          min={1}
          value={threshold}
          onChange={(e) =>
            setThreshold(Math.max(1, parseInt(e.target.value) || 1))
          }
          className="w-24 rounded-lg border border-black/15 bg-transparent px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-white/20"
        />
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
        {status === "submitting" ? "Signing up…" : "Get alerts"}
      </button>
    </form>
  );
}
