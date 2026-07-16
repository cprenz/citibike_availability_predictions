"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";

const MiniMap = dynamic(() => import("@/components/MiniMap"), { ssr: false });

const HORIZONS = [
  { minutes: 60, label: "1 hr" },
  { minutes: 180, label: "3 hr" },
  { minutes: 360, label: "6 hr" },
  { minutes: 720, label: "12 hr" },
  { minutes: 1440, label: "24 hr" },
  { minutes: 2880, label: "Multi-day" },
];

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

export default function StationDetail({ stationId }: { stationId: string }) {
  const [stations, setStations] = useState<Station[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/stations")
      .then((r) => {
        if (!r.ok) throw new Error(`API returned ${r.status}`);
        return r.json();
      })
      .then((data: Station[]) => setStations(data))
      .catch((e: Error) => setError(e.message));
  }, []);

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

  const station = stations.find((s) => s.station_id === stationId);

  if (!station) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-4 p-16 text-center">
        <h1 className="text-2xl font-semibold">Station not found</h1>
        <p className="max-w-md text-zinc-600 dark:text-zinc-400">
          We couldn&apos;t find a station with id &ldquo;{stationId}&rdquo;.
          It may no longer be active.
        </p>
        <a href="/" className="text-blue-600 hover:underline dark:text-blue-400">
          Back to the map
        </a>
      </main>
    );
  }

  const oneHour = station.horizons.find((h) => h.horizon_minutes === 60);

  return (
    <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col gap-8 p-6 md:p-10">
      <div className="flex flex-col gap-1">
        <a href="/" className="text-sm text-blue-600 hover:underline dark:text-blue-400">
          &larr; Back to map
        </a>
        <h1 className="text-2xl font-semibold tracking-tight md:text-3xl">
          {station.station_name}
        </h1>
        <p className="text-sm text-zinc-500">{station.capacity} docks</p>
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-[1fr_260px]">
        {/* Live snapshot + CTA */}
        <div className="flex flex-col gap-4 rounded-xl border border-black/10 p-5 dark:border-white/10">
          <h2 className="text-sm font-medium text-zinc-500">
            Right now (1 hr forecast)
          </h2>
          {oneHour ? (
            <div className="flex items-baseline gap-3">
              <span className="text-4xl font-bold">
                {Math.round(oneHour.predicted_value_lgbm)}
              </span>
              <span className="text-sm text-zinc-500">bikes predicted</span>
              <span className={`ml-auto text-sm font-semibold ${probColor(oneHour.predicted_prob_logistic)}`}>
                {Math.round(oneHour.predicted_prob_logistic * 100)}% &mdash; {probLabel(oneHour.predicted_prob_logistic)}
              </span>
            </div>
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

        {/* Mini map inset */}
        <div className="h-48 md:h-full">
          <MiniMap lat={station.lat} lon={station.lon} name={station.station_name} />
        </div>
      </div>

      {/* 6-horizon card row */}
      <div>
        <h2 className="mb-3 text-sm font-medium text-zinc-500">
          Full forecast breakdown
        </h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-6">
          {HORIZONS.map((h) => {
            const hz = station.horizons.find((x) => x.horizon_minutes === h.minutes);
            return (
              <div
                key={h.minutes}
                className="flex flex-col gap-1 rounded-xl border border-black/10 p-3 dark:border-white/10"
              >
                <span className="text-xs font-medium text-zinc-500">{h.label}</span>
                {hz ? (
                  <>
                    <span className="text-2xl font-bold">
                      {Math.round(hz.predicted_value_lgbm)}
                    </span>
                    <span className="text-[11px] text-zinc-500">
                      range {Math.max(0, Math.round(hz.pi_lower))}&ndash;{Math.round(hz.pi_upper)}
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
