"use client";

import { useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

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
  if (prob >= 0.7) return "#22c55e";
  if (prob >= 0.4) return "#f59e0b";
  return "#ef4444";
}

function buildGeoJSON(
  stations: Station[],
  horizonMinutes: number
): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: stations.map((s) => {
      const hz = s.horizons.find((h) => h.horizon_minutes === horizonMinutes);
      return {
        type: "Feature",
        geometry: { type: "Point", coordinates: [s.lon, s.lat] },
        properties: {
          id: s.station_id,
          name: s.station_name,
          capacity: s.capacity,
          prob: hz?.predicted_prob_logistic ?? 0,
          bikes: hz ? Math.round(hz.predicted_value_lgbm) : 0,
          horizons: JSON.stringify(s.horizons),
        },
      };
    }),
  };
}

function buildPopupHTML(name: string, capacity: number, horizons: HorizonData[], stationId: string): string {
  const rows = HORIZONS.map((h) => {
    const hz = horizons.find((x) => x.horizon_minutes === h.minutes);
    const prob = hz != null ? `${Math.round(hz.predicted_prob_logistic * 100)}%` : "--";
    const bikes = hz != null ? Math.round(hz.predicted_value_lgbm) : "--";
    const color = hz != null ? probColor(hz.predicted_prob_logistic) : "#666";
    return `<tr>
      <td style="padding:3px 8px;color:#aaa">${h.label}</td>
      <td style="padding:3px 8px;text-align:right">${bikes}</td>
      <td style="padding:3px 8px;text-align:right;color:${color};font-weight:600">${prob}</td>
    </tr>`;
  }).join("");

  return `
    <div style="font-family:system-ui,sans-serif;padding:4px 2px">
      <div style="font-weight:700;font-size:13px;margin-bottom:3px">${name}</div>
      <div style="font-size:11px;color:#999;margin-bottom:10px">Capacity: ${capacity} docks</div>
      <table style="width:100%;font-size:12px;border-collapse:collapse">
        <tr style="font-size:11px;color:#777">
          <th style="text-align:left;padding:3px 8px">Horizon</th>
          <th style="text-align:right;padding:3px 8px">Bikes</th>
          <th style="text-align:right;padding:3px 8px">Prob.</th>
        </tr>
        ${rows}
      </table>
      <a href="/signup?station_id=${stationId}"
         style="display:block;margin-top:12px;padding:8px;background:#2563eb;color:#fff;
                text-align:center;border-radius:6px;text-decoration:none;
                font-size:12px;font-weight:600">
        Get alerts for this station
      </a>
    </div>
  `;
}

export default function Map() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<mapboxgl.Map | null>(null);
  const popupRef = useRef<mapboxgl.Popup | null>(null);
  const mapReadyRef = useRef(false);

  const [selectedHorizon, setSelectedHorizon] = useState(60);
  const [stations, setStations] = useState<Station[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fetch station predictions
  useEffect(() => {
    fetch("/api/stations")
      .then((r) => {
        if (!r.ok) throw new Error(`API returned ${r.status}`);
        return r.json();
      })
      .then((data: Station[]) => {
        setStations(data);
        setLoading(false);
      })
      .catch((e: Error) => {
        setError(e.message);
        setLoading(false);
      });
  }, []);

  // Initialize map once
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    mapboxgl.accessToken = process.env.NEXT_PUBLIC_MAPBOX_TOKEN!;

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/dark-v11",
      center: [-73.985, 40.748],
      zoom: 12,
    });

    map.addControl(new mapboxgl.NavigationControl(), "top-right");
    popupRef.current = new mapboxgl.Popup({
      closeButton: true,
      maxWidth: "280px",
    });

    map.on("load", () => {
      map.addSource("stations", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });

      map.addLayer({
        id: "stations-circle",
        type: "circle",
        source: "stations",
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["zoom"],
            10, 4,
            15, 9,
          ],
          "circle-color": [
            "case",
            [">=", ["get", "prob"], 0.7], "#22c55e",
            [">=", ["get", "prob"], 0.4], "#f59e0b",
            "#ef4444",
          ],
          "circle-stroke-width": 1,
          "circle-stroke-color": "rgba(255,255,255,0.25)",
          "circle-opacity": 0.9,
        },
      });

      map.on("click", "stations-circle", (e) => {
        const feat = e.features?.[0];
        if (!feat) return;
        const p = feat.properties as {
          id: string;
          name: string;
          capacity: number;
          horizons: string;
        };
        const horizons: HorizonData[] = JSON.parse(p.horizons);
        const coords = (
          feat.geometry as { coordinates: [number, number] }
        ).coordinates;

        popupRef.current!
          .setLngLat(coords)
          .setHTML(buildPopupHTML(p.name, p.capacity, horizons, p.id))
          .addTo(map);
      });

      map.on("mouseenter", "stations-circle", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "stations-circle", () => {
        map.getCanvas().style.cursor = "";
      });

      mapReadyRef.current = true;
      // Dispatch a custom event so the data effect can re-run
      map.fire("map-ready" as Parameters<typeof map.fire>[0]);
    });

    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      mapReadyRef.current = false;
    };
  }, []);

  // Update GeoJSON source whenever stations or horizon changes
  useEffect(() => {
    const apply = () => {
      if (!mapRef.current || stations.length === 0) return;
      const src = mapRef.current.getSource("stations") as mapboxgl.GeoJSONSource | undefined;
      src?.setData(buildGeoJSON(stations, selectedHorizon));
    };

    if (mapReadyRef.current) {
      apply();
    } else if (mapRef.current) {
      mapRef.current.once("map-ready" as Parameters<typeof mapRef.current.once>[0], apply);
    }
  }, [stations, selectedHorizon]);

  return (
    <div className="relative flex-1 flex flex-col" style={{ minHeight: 0 }}>
      {/* Horizon tabs */}
      <div className="absolute top-3 left-1/2 z-10 -translate-x-1/2 flex gap-1 rounded-lg bg-black/70 p-1 backdrop-blur">
        {HORIZONS.map((h) => (
          <button
            key={h.minutes}
            onClick={() => setSelectedHorizon(h.minutes)}
            className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
              selectedHorizon === h.minutes
                ? "bg-blue-600 text-white"
                : "text-zinc-300 hover:text-white"
            }`}
          >
            {h.label}
          </button>
        ))}
      </div>

      {/* Legend */}
      <div className="absolute bottom-8 left-3 z-10 rounded-lg bg-black/70 p-3 text-xs text-white backdrop-blur">
        <div className="mb-1.5 flex items-center gap-2">
          <span className="h-3 w-3 shrink-0 rounded-full bg-green-500" />
          Likely available (&ge;70%)
        </div>
        <div className="mb-1.5 flex items-center gap-2">
          <span className="h-3 w-3 shrink-0 rounded-full bg-amber-500" />
          Uncertain (40–70%)
        </div>
        <div className="flex items-center gap-2">
          <span className="h-3 w-3 shrink-0 rounded-full bg-red-500" />
          Likely empty (&lt;40%)
        </div>
      </div>

      {loading && (
        <div className="absolute inset-0 z-20 flex items-center justify-center">
          <span className="rounded-lg bg-black/70 px-4 py-2 text-sm text-white backdrop-blur">
            Loading stations...
          </span>
        </div>
      )}

      {error && (
        <div className="absolute left-1/2 top-14 z-10 -translate-x-1/2 rounded-lg bg-red-900/80 px-3 py-2 text-xs text-white">
          Error: {error}
        </div>
      )}

      <div ref={containerRef} className="flex-1" style={{ minHeight: 0 }} />
    </div>
  );
}
