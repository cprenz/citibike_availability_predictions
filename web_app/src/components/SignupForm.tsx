"use client";

import { useEffect, useMemo, useState } from "react";

const HORIZONS = [
  { minutes: 60, label: "1 hour before" },
  { minutes: 180, label: "3 hours before" },
  { minutes: 360, label: "6 hours before" },
  { minutes: 720, label: "12 hours before" },
  { minutes: 1440, label: "24 hours before" },
  { minutes: 2880, label: "2 days before" },
];

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

function alertTimeLabel(targetTime: string, horizonMinutes: number): string {
  const [h, m] = targetTime.split(":").map(Number);
  const totalMinutes = h * 60 + m - horizonMinutes;
  const wrapped = ((totalMinutes % 1440) + 1440) % 1440;
  const alertH = Math.floor(wrapped / 60);
  const alertM = wrapped % 60;
  const period = alertH >= 12 ? "PM" : "AM";
  const display = alertH % 12 === 0 ? 12 : alertH % 12;
  return `alert at ${display}:${alertM.toString().padStart(2, "0")} ${period}`;
}

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
  const [targetTime, setTargetTime] = useState("");
  const [horizons, setHorizons] = useState<number[]>([60, 180]);
  const [threshold, setThreshold] = useState(1);

  const [status, setStatus] = useState<"idle" | "submitting" | "done">("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

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

  function toggleHorizon(minutes: number) {
    setHorizons((prev) =>
      prev.includes(minutes) ? prev.filter((m) => m !== minutes) : [...prev, minutes]
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
      setErrorMsg("Pick at least one alert timing.");
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
          target_time: targetTime || null,
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
        <div className="mb-2 text-2xl">&#10003;</div>
        <h2 className="mb-2 text-xl font-semibold">You&apos;re signed up</h2>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          We&apos;ll alert you about bike availability
          {selectedStationName ? ` at ${selectedStationName}` : ""}
          {targetTime ? ` around ${targetTime}` : ""}.
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
      {/* Contact */}
      <div>
        <label className="mb-1 block text-sm font-medium" htmlFor="email">
          Email <span className="text-zinc-500">(optional)</span>
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
          At least one of email or phone is required.
        </p>
      </div>

      {/* Station */}
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

      {/* Target time */}
      <div>
        <label className="mb-1 block text-sm font-medium" htmlFor="target-time">
          When do you need to be at the station?{" "}
          <span className="text-zinc-500">(optional)</span>
        </label>
        <select
          id="target-time"
          value={targetTime}
          onChange={(e) => setTargetTime(e.target.value)}
          className="w-full rounded-lg border border-black/15 bg-transparent px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-white/20"
        >
          <option value="">Select a time</option>
          {TARGET_TIME_SLOTS.map((slot) => (
            <option key={slot.value} value={slot.value}>
              {slot.label}
            </option>
          ))}
        </select>
        {targetTime && (
          <p className="mt-1 text-xs text-zinc-500">
            We&apos;ll alert you before {TARGET_TIME_SLOTS.find((s) => s.value === targetTime)?.label} based on the lead times you choose below.
          </p>
        )}
      </div>

      {/* Horizons */}
      <div>
        <span className="mb-2 block text-sm font-medium">
          {targetTime ? "Alert me this far in advance" : "Alert me for"}
        </span>
        <div className="grid grid-cols-2 gap-2">
          {HORIZONS.map((h) => (
            <label
              key={h.minutes}
              className="flex flex-col gap-0.5 rounded-lg border border-black/10 px-3 py-2 text-sm dark:border-white/15 cursor-pointer"
            >
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={horizons.includes(h.minutes)}
                  onChange={() => toggleHorizon(h.minutes)}
                  className="accent-blue-600"
                />
                <span>{h.label}</span>
              </div>
              {targetTime && (
                <span className="pl-5 text-xs text-zinc-400">
                  {alertTimeLabel(targetTime, h.minutes)}
                </span>
              )}
            </label>
          ))}
        </div>
      </div>

      {/* Threshold */}
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
        {status === "submitting" ? "Signing up..." : "Get alerts"}
      </button>
    </form>
  );
}
