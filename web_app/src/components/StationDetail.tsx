"use client";

import { useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";

const MiniMap = dynamic(() => import("@/components/MiniMap"), { ssr: false });

const ANCHORS = [60, 180, 360, 720, 1440, 2880];

const HORIZON_LABELS: Record<number, string> = {
  60: "1 hr",
  180: "3 hr",
  360: "6 hr",
  720: "12 hr",
  1440: "24 hr",
  2880: "Multi-day",
};

type HorizonData = {
  horizon_minutes: number;
  predicted_prob_logistic: number;
  predicted_value_lgbm: number;
  pi_lower: number;
  pi_upper: number;
};

type Station = {
  station_id: string;
  station_name: string;
  lat: number;
  lon: number;
  capacity: number;
  horizons: HorizonData[];
};

type Slot = { label: string; minutesFromNow: number };

type Prediction = {
  prob: number;
  bikes: number;
  piLower: number;
  piUpper: number;
  isAnchor: boolean;
};

function probColor(prob: number): string {
  if (prob >= 0.7) return "text-green-500";
  if (prob >= 0.4) return "text-amber-500";
  return "text-red-500";
}

function probLabel(prob: number): string {
  if (prob >= 0.7) return "Likely available";
  if (prob >= 0.4) return "Uncertain";
  return "Likely empty";
}

function buildTimeSlots(): Slot[] {
  const now = new Date();
  const msNow = now.getTime();

  // Round up to the next 30-min boundary
  const rem = now.getMinutes() % 30;
  const msToNext =
    (rem === 0 ? 30 : 30 - rem) * 60_000 -
    now.getSeconds() * 1000 -
    now.getMilliseconds();
  const startMs = msNow + msToNext;

  const slots: Slot[] = [{ label: "Now", minutesFromNow: 60 }];

  for (let i = 0; i <= 96; i++) {
    const slotMs = startMs + i * 30 * 60_000;
    const minutesFromNow = Math.round((slotMs - msNow) / 60_000);
    if (minutesFromNow > 2880) break;

    const d = new Date(slotMs);
    const isToday = d.toDateString() === now.toDateString();
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const day = d.toLocaleDateString([], { weekday: "short" });

    slots.push({
      label: isToday ? time : `${day} ${time}`,
      minutesFromNow,
    });
  }

  return slots;
}

function interpolate(horizons: HorizonData[], targetMinutes: number): Prediction | null {
  const clamped = Math.max(60, Math.min(2880, targetMinutes));
  const isAnchor = ANCHORS.includes(clamped);

  if (isAnchor) {
    const h = horizons.find((x) => x.horizon_minutes === clamped);
    return h
      ? { prob: h.predicted_prob_logistic, bikes: h.predicted_value_lgbm, piLower: h.pi_lower, piUpper: h.pi_upper, isAnchor: true }
      : null;
  }

  let lower = ANCHORS[0], upper = ANCHORS[ANCHORS.length - 1];
  for (let i = 0; i < ANCHORS.length - 1; i++) {
    if (clamped >= ANCHORS[i] && clamped <= ANCHORS[i + 1]) {
      lower = ANCHORS[i];
      upper = ANCHORS[i + 1];
      break;
    }
  }

  const lo = horizons.find((x) => x.horizon_minutes === lower);
  const hi = horizons.find((x) => x.horizon_minutes === upper);
  if (!lo || !hi) return null;

  const t = (clamped - lower) / (upper - lower);
  return {
    prob: lo.predicted_prob_logistic * (1 - t) + hi.predicted_prob_logistic * t,
    bikes: lo.predicted_value_lgbm * (1 - t) + hi.predicted_value_lgbm * t,
    piLower: lo.pi_lower * (1 - t) + hi.pi_lower * t,
    piUpper: lo.pi_upper * (1 - t) + hi.pi_upper * t,
    isAnchor: false,
  };
}

export default function StationDetail({ stationId }: { stationId: string }) {
  const [stations, setStations] = useState<Station[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [slotIndex, setSlotIndex] = useState(0);

  const timeSlots = useMemo(() => buildTimeSlots(), []);

  useEffect(() => {
    fetch("/api/stations")
      .then((r) => {
        if (!r.ok) throw new Error(`API returned ${r.status}`);
        return r.json();
      })
      .then((data: Station[]) => setStations(data))
      .catch((e: Error) => setError(e.message));
  }, []);

  const station = useMemo(
    () => stations?.find((s) => s.station_id === stationId) ?? null,
    [stations, stationId]
  );

  const prediction = useMemo(
    () => (station ? interpolate(station.horizons, timeSlots[slotIndex].minutesFromNow) : null),
    [station, timeSlots, slotIndex]
  );

  if (error) {
    return (
      <main className="flex flex-1 items-center justify-center p-16">
        <p className="text-red-500">Error loading station: {error}</p>
      </main>
    );
  }

  if (!stations) {
    return (
      <main className="flex flex-1 items-center justify-center p-16">
        <p className="text-zinc-500">Loading station...</p>
      </main>
    );
  }

  if (!station) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-4 p-16 text-center">
        <h1 className="text-2xl font-semibold">Station not found</h1>
        <p className="max-w-md text-zinc-600 dark:text-zinc-400">
          We couldn&apos;t find a station with id &ldquo;{stationId}&rdquo;. It
          may no longer be active.
        </p>
        <a href="/" className="text-blue-600 hover:underline dark:text-blue-400">
          Back to the map
        </a>
      </main>
    );
  }

  return (
    <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col gap-8 p-6 md:p-10">
      {/* Header */}
      <div className="flex flex-col gap-1">
        <a href="/" className="text-sm text-blue-600 hover:underline dark:text-blue-400">
          &larr; Back to map
        </a>
        <h1 className="text-2xl font-semibold tracking-tight md:text-3xl">
          {station.station_name}
        </h1>
        <p className="text-sm text-zinc-500">{station.capacity} docks</p>
      </div>

      {/* Time picker + mini map */}
      <div className="grid grid-cols-1 gap-6 md:grid-cols-[1fr_260px]">
        <div className="flex flex-col gap-4 rounded-xl border border-black/10 p-5 dark:border-white/10">
          {/* Departure time picker */}
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="departure-time"
              className="text-sm font-medium text-zinc-500"
            >
              When are you leaving?
            </label>
            <select
              id="departure-time"
              value={slotIndex}
              onChange={(e) => setSlotIndex(Number(e.target.value))}
              className="w-full rounded-lg border border-black/15 bg-transparent px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-white/20"
            >
              {timeSlots.map((slot, i) => (
                <option key={i} value={i}>
                  {slot.label}
                </option>
              ))}
            </select>
          </div>

          {/* Interpolated prediction */}
          {prediction ? (
            <>
              <div className="flex items-baseline gap-3">
                <span className="text-4xl font-bold">
                  {Math.round(prediction.bikes)}
                </span>
                <span className="text-sm text-zinc-500">bikes predicted</span>
                <span
                  className={`ml-auto text-sm font-semibold ${probColor(prediction.prob)}`}
                >
                  {Math.round(prediction.prob * 100)}%{" "}
                  &mdash; {probLabel(prediction.prob)}
                </span>
              </div>
              <p className="text-xs text-zinc-400">
                range {Math.max(0, Math.round(prediction.piLower))}&ndash;
                {Math.round(prediction.piUpper)} bikes
                {!prediction.isAnchor && (
                  <span className="ml-1 text-zinc-400">· interpolated</span>
                )}
              </p>
            </>
          ) : (
            <p className="text-sm text-zinc-500">No prediction available.</p>
          )}

          <a
            href={`/signup?station_id=${station.station_id}`}
            className="mt-2 inline-block rounded-lg bg-blue-600 px-4 py-2 text-center text-sm font-semibold text-white hover:bg-blue-700"
          >
            Get alerts for this station
          </a>
        </div>

        <div className="h-48 md:h-full">
          <MiniMap lat={station.lat} lon={station.lon} name={station.station_name} />
        </div>
      </div>

      {/* 6-horizon reference cards */}
      <div>
        <h2 className="mb-3 text-sm font-medium text-zinc-500">
          Full forecast breakdown
        </h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-6">
          {ANCHORS.map((minutes) => {
            const hz = station.horizons.find((x) => x.horizon_minutes === minutes);
            return (
              <div
                key={minutes}
                className="flex flex-col gap-1 rounded-xl border border-black/10 p-3 dark:border-white/10"
              >
                <span className="text-xs font-medium text-zinc-500">
                  {HORIZON_LABELS[minutes]}
                </span>
                {hz ? (
                  <>
                    <span className="text-2xl font-bold">
                      {Math.round(hz.predicted_value_lgbm)}
                    </span>
                    <span className="text-[11px] text-zinc-500">
                      range {Math.max(0, Math.round(hz.pi_lower))}&ndash;
                      {Math.round(hz.pi_upper)}
                    </span>
                    <span className={`text-xs font-semibold ${probColor(hz.predicted_prob_logistic)}`}>
                      {Math.round(hz.predicted_prob_logistic * 100)}%
                    </span>
                  </>
                ) : (
                  <span className="text-sm text-zinc-500">&mdash;</span>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </main>
  );
}
