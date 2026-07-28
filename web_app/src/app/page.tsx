"use client";
import { useState } from "react";
import dynamic from "next/dynamic";

const PredictionMap = dynamic(() => import("@/components/Map"),       { ssr: false });
const RideExplorer  = dynamic(() => import("@/components/ThreeDMap"), { ssr: false });

type View = "predictions" | "explorer";

export default function Home() {
  const [view, setView] = useState<View>("predictions");

  return (
    <div className="flex-1 flex flex-col" style={{ minHeight: 0 }}>
      {/* View toggle bar */}
      <div className="flex items-center gap-1 bg-zinc-900 border-b border-zinc-700 px-4 py-1.5">
        {(["predictions", "explorer"] as const).map(v => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
              view === v
                ? "bg-blue-600 text-white"
                : "text-zinc-400 hover:text-zinc-100"
            }`}
          >
            {v === "predictions" ? "Live Predictions" : "Ride Explorer"}
          </button>
        ))}
      </div>

      <div className="flex-1 flex flex-col" style={{ minHeight: 0 }}>
        {view === "predictions" ? <PredictionMap /> : <RideExplorer />}
      </div>
    </div>
  );
}
