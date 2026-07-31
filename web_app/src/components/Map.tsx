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

function buildTimeSlotOptions(placeholder: string): string {
  const opts: string[] = [`<option value="">${placeholder}</option>`];
  for (let h = 0; h < 24; h++) {
    for (let m = 0; m < 60; m += 30) {
      const value = `${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}`;
      const period = h >= 12 ? "PM" : "AM";
      const displayH = h % 12 === 0 ? 12 : h % 12;
      const label = `${displayH}:${m.toString().padStart(2, "0")} ${period}`;
      opts.push(`<option value="${value}">${label}</option>`);
    }
  }
  return opts.join("");
}

const INPUT_STYLE =
  "width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #d1d5db;" +
  "border-radius:6px;font-size:12px;margin-bottom:6px;outline:none;color:#111;background:#fff;font-family:system-ui,sans-serif";

function buildPopupHTML(stationId: string, name: string, capacity: number, horizons: HorizonData[]): string {
  const rows = HORIZONS.map((h) => {
    const hz = horizons.find((x) => x.horizon_minutes === h.minutes);
    const probNum = hz != null ? Math.round(hz.predicted_prob_logistic * 100) : 0;
    const prob = hz != null ? `${probNum}%` : "--";
    const bikes = hz != null ? Math.round(hz.predicted_value_lgbm) : "--";
    const color = hz != null ? probColor(hz.predicted_prob_logistic) : "#666";
    const bar = hz != null
      ? `<div style="background:#e5e7eb;border-radius:3px;overflow:hidden;width:72px;height:7px;display:inline-block;vertical-align:middle">
           <div style="width:${probNum}%;height:100%;background:${color}"></div>
         </div>`
      : `<div style="width:72px;height:7px;display:inline-block"></div>`;
    return `<tr>
      <td style="padding:3px 8px;color:#444">${h.label}</td>
      <td style="padding:3px 8px">${bar}</td>
      <td style="padding:3px 8px;text-align:right;color:${color};font-weight:600">${prob}</td>
      <td style="padding:3px 8px;text-align:right;color:#111;font-weight:600">${bikes}</td>
    </tr>`;
  }).join("");

  return `
    <div style="font-family:system-ui,sans-serif;padding:4px 2px">
      <div style="font-weight:700;font-size:13px;margin-bottom:3px;color:#000">${name}</div>
      <div style="font-size:11px;color:#666;margin-bottom:10px">Capacity: ${capacity} docks</div>
      <table style="width:100%;font-size:12px;border-collapse:collapse">
        <tr style="font-size:11px;color:#666">
          <th style="text-align:left;padding:3px 8px">Horizon</th>
          <th style="padding:3px 8px"></th>
          <th style="text-align:right;padding:3px 8px">Prob.</th>
          <th style="text-align:right;padding:3px 8px">Bikes</th>
        </tr>
        ${rows}
      </table>
      <div class="popup-form-wrap" style="margin-top:12px;border-top:1px solid #e5e7eb;padding-top:10px">
        <div style="font-size:11px;font-weight:700;color:#374151;margin-bottom:8px;letter-spacing:0.02em">
          GET ALERTS FOR THIS STATION
        </div>
        <input class="popup-email" type="email" placeholder="Email"
          style="${INPUT_STYLE}" autocomplete="email" />
        <select class="popup-time"
          style="${INPUT_STYLE}cursor:pointer">
          ${buildTimeSlotOptions("When do you want the alert email?")}
        </select>
        <select class="popup-pred-time"
          style="${INPUT_STYLE}margin-bottom:8px;cursor:pointer">
          ${buildTimeSlotOptions("For what time? (optional)")}
        </select>
        <button class="popup-submit"
          style="width:100%;padding:9px;background:#2563eb;color:#fff;border:none;
                 border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;
                 font-family:system-ui,sans-serif">
          Get alerts
        </button>
        <div class="popup-msg" style="font-size:11px;margin-top:6px;text-align:center;min-height:16px"></div>
      </div>
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

  const selectedHorizonRef = useRef(selectedHorizon);
  useEffect(() => {
    selectedHorizonRef.current = selectedHorizon;
  }, [selectedHorizon]);

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

      let closeTimer: number | null = null;

      const closePopup = () => {
        popupRef.current?.remove();
        popupRef.current = null;
      };
      const cancelClose = () => {
        if (closeTimer !== null) {
          window.clearTimeout(closeTimer);
          closeTimer = null;
        }
      };
      const scheduleClose = () => {
        cancelClose();
        closeTimer = window.setTimeout(() => {
          // Don't close while the user is typing in the popup form
          const active = document.activeElement;
          const popupEl = popupRef.current?.getElement();
          if (popupEl && active && popupEl.contains(active)) return;
          closePopup();
        }, 300);
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

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (window as any).gtag?.("event", "station_viewed", { station_id: p.id, station_name: p.name });
        cancelClose();
        popupRef.current?.remove();
        const popup = new mapboxgl.Popup({
          closeButton: true,
          closeOnClick: false,
          focusAfterOpen: false,
          maxWidth: "290px",
        });
        popupRef.current = popup;
        popup
          .setLngLat(coords)
          .setHTML(buildPopupHTML(p.id, p.name, p.capacity, horizons))
          .addTo(map);

        popup.on("close", () => {
          if (popupRef.current === popup) popupRef.current = null;
        });

        const el = popup.getElement();
        el.addEventListener("mouseenter", cancelClose);
        el.addEventListener("mouseleave", scheduleClose);

        // Wire up the inline alert form
        const submitBtn = el.querySelector(".popup-submit") as HTMLButtonElement | null;
        const formWrap = el.querySelector(".popup-form-wrap") as HTMLDivElement | null;

        submitBtn?.addEventListener("click", async () => {
          // Query at click time so Mapbox rendering is definitely settled
          const emailEl = el.querySelector(".popup-email") as HTMLInputElement | null;
          const timeEl = el.querySelector(".popup-time") as HTMLSelectElement | null;
          const predTimeEl = el.querySelector(".popup-pred-time") as HTMLSelectElement | null;
          const msgEl = el.querySelector(".popup-msg") as HTMLDivElement | null;
          const email = emailEl?.value.trim() ?? "";
          const targetTime = timeEl?.value ?? "";
          const predictionTime = predTimeEl?.value ?? "";

          if (!email) {
            if (msgEl) { msgEl.style.color = "#dc2626"; msgEl.textContent = "Enter your email address."; }
            return;
          }

          if (submitBtn) { submitBtn.textContent = "Signing up..."; submitBtn.disabled = true; }
          if (msgEl) msgEl.textContent = "";

          try {
            const res = await fetch("/api/subscribe", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                email: email || null,
                station_id: p.id,
                station_name: p.name,
                target_time: targetTime || null,
                prediction_time: predictionTime || null,
                horizons: [60, 180, 360, 720, 1440, 2880],
                threshold: 1,
              }),
            });
            const data = await res.json();
            if (!res.ok) {
              if (msgEl) { msgEl.style.color = "#dc2626"; msgEl.textContent = data.error ?? "Something went wrong."; }
              if (submitBtn) { submitBtn.textContent = "Get alerts"; submitBtn.disabled = false; }
            } else {
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              (window as any).fbq?.("track", "Lead");
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              (window as any).gtag?.("event", "signup_complete", { station_id: p.id, station_name: p.name });
              if (formWrap) {
                formWrap.innerHTML = `
                  <div style="text-align:center;padding:12px 0">
                    <div style="font-size:20px;margin-bottom:4px">&#10003;</div>
                    <div style="font-weight:700;color:#16a34a;font-size:13px">You're signed up!</div>
                    <div style="font-size:11px;color:#555;margin-top:4px">Check your email for a confirmation.</div>
                  </div>`;
                // Defer past the mouseleave event the popup resize triggers;
                // scheduleClose() inside mouseleave would cancel our 4s timer otherwise.
                window.setTimeout(() => {
                  cancelClose();
                  window.setTimeout(() => closePopup(), 4000);
                }, 0);
              }
            }
          } catch {
            if (msgEl) { msgEl.style.color = "#dc2626"; msgEl.textContent = "Network error. Try again."; }
            if (submitBtn) { submitBtn.textContent = "Get alerts"; submitBtn.disabled = false; }
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

      map.on("click", (e) => {
        const hits = map.queryRenderedFeatures(e.point, { layers: ["stations-circle"] });
        if (hits.length === 0) closePopup();
      });

      mapReadyRef.current = true;
      map.fire("map-ready" as Parameters<typeof map.fire>[0]);
    });

    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      mapReadyRef.current = false;
    };
  }, []);

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
            onClick={() => {
                setSelectedHorizon(h.minutes);
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                (window as any).gtag?.("event", "horizon_selected", { horizon: h.label });
              }}
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
