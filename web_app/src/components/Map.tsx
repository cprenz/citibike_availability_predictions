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

function buildPopupHTML(name: string, capacity: number, horizons: HorizonData[]): string {
  const rows = HORIZONS.map((h) => {
    const hz = horizons.find((x) => x.horizon_minutes === h.minutes);
    const prob = hz != null ? `${Math.round(hz.predicted_prob_logistic * 100)}%` : "--";
    const bikes = hz != null ? Math.round(hz.predicted_value_lgbm) : "--";
    const color = hz != null ? probColor(hz.predicted_prob_logistic) : "#666";
    return `<tr>
      <td style="padding:3px 8px;color:#333">${h.label}</td>
      <td style="padding:3px 8px;text-align:right;color:#111;font-weight:600">${bikes}</td>
      <td style="padding:3px 8px;text-align:right;color:${color};font-weight:600">${prob}</td>
    </tr>`;
  }).join("");

  return `
    <div style="font-family:system-ui,sans-serif;padding:4px 2px">
      <div style="font-weight:700;font-size:13px;margin-bottom:3px;color:#000">${name}</div>
      <div style="font-size:11px;color:#666;margin-bottom:10px">Capacity: ${capacity} docks</div>
      <table style="width:100%;font-size:12px;border-collapse:collapse">
        <tr style="font-size:11px;color:#555">
          <th style="text-align:left;padding:3px 8px">Horizon</th>
          <th style="text-align:right;padding:3px 8px">Bikes</th>
          <th style="text-align:right;padding:3px 8px">Prob.</th>
        </tr>
        ${rows}
      </table>
      <form data-signup style="margin-top:12px;display:flex;flex-direction:column;gap:6px">
        <input type="email" name="email" placeholder="you@email.com" required
          style="padding:7px 8px;border:1px solid #ccc;border-radius:6px;font-size:12px;color:#111" />
        <button type="submit"
          style="padding:8px;background:#2563eb;color:#fff;border:none;border-radius:6px;
                 font-size:12px;font-weight:600;cursor:pointer">
          Get alerts for this station
        </button>
        <div data-status style="font-size:11px;text-align:center;min-height:14px"></div>
      </form>
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

  // The map init effect runs once, so its handlers would capture a stale
  // selectedHorizon. Mirror it in a ref the hover/subscribe handler can read live.
  const selectedHorizonRef = useRef(selectedHorizon);
  useEffect(() => {
    selectedHorizonRef.current = selectedHorizon;
  }, [selectedHorizon]);

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

      // Hover UX: open a popup on mouseenter. Because the popup also holds an
      // interactive signup form, we can't close it the instant the pointer
      // leaves the dot — a short timer keeps it alive while the user moves onto
      // the popup, and focusing the email field "pins" it open entirely.
      let closeTimer: number | null = null;
      let pinned = false;

      const closePopup = () => {
        popupRef.current?.remove();
        popupRef.current = null;
        pinned = false;
      };
      const cancelClose = () => {
        if (closeTimer !== null) {
          window.clearTimeout(closeTimer);
          closeTimer = null;
        }
      };
      const scheduleClose = () => {
        if (pinned) return;
        cancelClose();
        closeTimer = window.setTimeout(closePopup, 300);
      };

      const showPopup = (feat: mapboxgl.MapGeoJSONFeature) => {
        const p = feat.properties as {
          id: string;
          name: string;
          capacity: number;
          horizons: string;
        };
        const horizons: HorizonData[] = JSON.parse(p.horizons);
        const coords = (feat.geometry as GeoJSON.Point).coordinates as [number, number];

        cancelClose();
        popupRef.current?.remove();
        const popup = new mapboxgl.Popup({
          closeButton: true,
          closeOnClick: false,
          focusAfterOpen: false,
          maxWidth: "280px",
        });
        popupRef.current = popup;
        popup
          .setLngLat(coords)
          .setHTML(buildPopupHTML(p.name, p.capacity, horizons))
          .addTo(map);

        const el = popup.getElement();
        el.addEventListener("mouseenter", cancelClose);
        el.addEventListener("mouseleave", scheduleClose);

        const form = el.querySelector("form[data-signup]") as HTMLFormElement | null;
        if (!form) return;
        const statusEl = form.querySelector("[data-status]") as HTMLElement;
        const emailEl = form.querySelector('input[name="email"]') as HTMLInputElement;
        const buttonEl = form.querySelector("button") as HTMLButtonElement;

        // Keep the popup open the whole time the user is filling out the form.
        form.addEventListener("focusin", () => {
          pinned = true;
          cancelClose();
        });

        form.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const email = emailEl.value.trim();
          if (!email) return;
          statusEl.textContent = "Signing up...";
          statusEl.style.color = "#666";
          try {
            const res = await fetch("/api/subscribe", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                email,
                station_id: p.id,
                horizons: [selectedHorizonRef.current],
                threshold: 1,
              }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error ?? "Signup failed");
            statusEl.textContent = "You're signed up!";
            statusEl.style.color = "#16a34a";
            emailEl.disabled = true;
            buttonEl.disabled = true;
          } catch (err) {
            statusEl.textContent =
              err instanceof Error ? err.message : "Signup failed";
            statusEl.style.color = "#dc2626";
          }
        });
      };

      map.on("mouseenter", "stations-circle", (e) => {
        map.getCanvas().style.cursor = "pointer";
        const feat = e.features?.[0];
        if (feat) showPopup(feat);
      });
      map.on("mouseleave", "stations-circle", () => {
        map.getCanvas().style.cursor = "";
        scheduleClose();
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
