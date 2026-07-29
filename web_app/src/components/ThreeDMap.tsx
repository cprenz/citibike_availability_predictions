"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

type StationRide = {
  station_id: string;
  station_name: string;
  lat: number;
  lon: number;
  capacity: number;
  borough: string;
  total_rides: number;
  avg_rides: number;
};

type Filters = {
  year: string;
  month: string;
  dow: string;
  hour: string;
  bikeType: string;
  riderType: string;
  metric: "avg" | "total";
  boroughs: Set<string>;
};

const YEARS = ["2019", "2021", "2022", "2023", "2024", "2025", "2026"];
const MONTHS = [
  { v: "all", l: "All months" },
  { v: "1", l: "January" }, { v: "2", l: "February" }, { v: "3", l: "March" },
  { v: "4", l: "April" },   { v: "5", l: "May" },      { v: "6", l: "June" },
  { v: "7", l: "July" },    { v: "8", l: "August" },   { v: "9", l: "September" },
  { v: "10", l: "October" },{ v: "11", l: "November" },{ v: "12", l: "December" },
];
const DAYS = [
  { v: "all", l: "All days" },
  { v: "0", l: "Sunday" }, { v: "1", l: "Monday" }, { v: "2", l: "Tuesday" },
  { v: "3", l: "Wednesday" }, { v: "4", l: "Thursday" }, { v: "5", l: "Friday" },
  { v: "6", l: "Saturday" },
];
const HOURS = [
  { v: "all", l: "All hours" },
  ...Array.from({ length: 24 }, (_, i) => ({
    v: String(i),
    l: i === 0 ? "12 AM ET" : i < 12 ? `${i} AM ET` : i === 12 ? "12 PM ET" : `${i - 12} PM ET`,
  })),
];
const ALL_BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island", "Jersey City", "Hoboken"];

// Per-borough bar colors for the native fill-extrusion layer (fill-extrusion-color
// reads a per-feature string). Same palette as the legend below.
const BOROUGH_HEX: Record<string, string> = {
  Manhattan:       "#3b82f6",
  Brooklyn:        "#f97316",
  Queens:          "#22c55e",
  Bronx:           "#a855f7",
  "Staten Island": "#eab308",
  "Jersey City":   "#94a3b8",
  Hoboken:         "#94a3b8",
};
const DEFAULT_HEX = "#64748b";

// fill-extrusion needs POLYGON footprints, so each station becomes a small square.
// ~22m half-size => ~44m-wide bars (slender poles, like manpopex's block extrusions).
const FOOTPRINT_HALF_M = 22;

function squareAround(lon: number, lat: number, halfM: number): number[][][] {
  const dLat = halfM / 111320;
  const dLon = halfM / (111320 * Math.cos((lat * Math.PI) / 180));
  return [[
    [lon - dLon, lat - dLat],
    [lon + dLon, lat - dLat],
    [lon + dLon, lat + dLat],
    [lon - dLon, lat + dLat],
    [lon - dLon, lat - dLat],
  ]];
}

// Build the fill-extrusion source data. Each feature carries its height (meters),
// color, and all station fields (so hover/click can read them straight off the
// feature — no external lookup, no stale closures).
function buildBarsGeoJSON(
  data: StationRide[],
  metric: "avg" | "total",
  elevScale: number,
  rankMap: Map<string, number>,
): GeoJSON.FeatureCollection {
  const features = data.map((d) => {
    const val = metric === "avg" ? d.avg_rides : d.total_rides;
    return {
      type: "Feature",
      geometry: { type: "Polygon", coordinates: squareAround(d.lon, d.lat, FOOTPRINT_HALF_M) },
      properties: {
        station_id:   d.station_id,
        station_name: d.station_name,
        lat:          d.lat,
        lon:          d.lon,
        capacity:     d.capacity,
        borough:      d.borough,
        total_rides:  d.total_rides,
        avg_rides:    d.avg_rides,
        rank:         rankMap.get(d.station_id) ?? 0,
        height:       Math.max(val * elevScale, 1),
        color:        BOROUGH_HEX[d.borough] ?? DEFAULT_HEX,
      },
    };
  });
  return { type: "FeatureCollection", features } as unknown as GeoJSON.FeatureCollection;
}

function propsToStation(p: Record<string, unknown>): StationRide {
  return {
    station_id:   String(p.station_id),
    station_name: String(p.station_name),
    lat:          Number(p.lat),
    lon:          Number(p.lon),
    capacity:     Number(p.capacity),
    borough:      String(p.borough),
    total_rides:  Number(p.total_rides),
    avg_rides:    Number(p.avg_rides),
  };
}

const LEGEND_COLORS: [string, [number, number, number]][] = [
  ["Manhattan",     [59,  130, 246]],
  ["Brooklyn",      [249, 115, 22]],
  ["Queens",        [34,  197, 94]],
  ["Bronx",         [168, 85,  247]],
  ["Staten Island", [234, 179, 8]],
  ["Jersey City / Hoboken", [148, 163, 184]],
];

function buildUrl(f: Filters): string {
  const p = new URLSearchParams({ year: f.year, metric: f.metric });
  if (f.month !== "all") p.set("month", f.month);
  if (f.dow   !== "all") p.set("dow", f.dow);
  if (f.hour  !== "all") p.set("hour", f.hour);
  if (f.bikeType  !== "all") p.set("bike_type",  f.bikeType);
  if (f.riderType !== "all") p.set("rider_type", f.riderType);
  if (f.boroughs.size > 0 && f.boroughs.size < ALL_BOROUGHS.length) {
    p.set("boroughs", [...f.boroughs].join(","));
  }
  return `/api/hourly-profile?${p}`;
}

const SELECT_CLS =
  "w-full rounded bg-zinc-800 border border-zinc-600 text-zinc-100 text-xs px-2 py-1.5 focus:outline-none focus:border-blue-500";
const RADIO_LABEL_CLS = "flex items-center gap-1.5 text-xs text-zinc-300 cursor-pointer";

export default function ThreeDMap() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef       = useRef<mapboxgl.Map | null>(null);

  const [filters, setFilters] = useState<Filters>({
    year: "2026", month: "all", dow: "all", hour: "all",
    bikeType: "all", riderType: "all", metric: "avg",
    boroughs: new Set(ALL_BOROUGHS),
  });
  const [data,         setData]         = useState<StationRide[]>([]);
  const [loading,      setLoading]      = useState(false);
  const [error,        setError]        = useState<string | null>(null);
  const [tooltip,      setTooltip]      = useState<{ x: number; y: number; d: StationRide } | null>(null);
  const [pinnedPopup,  setPinnedPopup]  = useState<{ x: number; y: number; d: StationRide; rank: number } | null>(null);
  const [mapLoaded,    setMapLoaded]    = useState(false);

  // Fixed elevation scale (meters): the p90 station gets a ~900m bar. Rendered via
  // native Mapbox fill-extrusion (same technique as manpopex), so the bars use
  // Mapbox's own camera and stay upright the way theirs do.
  const elevScale = (() => {
    if (!data.length) return 10;
    const vals = data.map(d => filters.metric === "avg" ? d.avg_rides : d.total_rides).sort((a, b) => a - b);
    const p90 = vals[Math.floor(vals.length * 0.9)] || 1;
    return 900 / p90;
  })();

  // Precompute per-borough rank for popup
  const boroughRankMap = (() => {
    const map = new Map<string, number>();
    const byBorough = new Map<string, StationRide[]>();
    for (const d of data) {
      if (!byBorough.has(d.borough)) byBorough.set(d.borough, []);
      byBorough.get(d.borough)!.push(d);
    }
    for (const [, stations] of byBorough) {
      const sorted = [...stations].sort((a, b) =>
        filters.metric === "avg" ? b.avg_rides - a.avg_rides : b.total_rides - a.total_rides
      );
      sorted.forEach((s, i) => map.set(s.station_id, i + 1));
    }
    return map;
  })();

  // Fetch on filter change
  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    fetch(buildUrl(filters), { signal: ctrl.signal })
      .then(r => { if (!r.ok) throw new Error(`API error ${r.status}`); return r.json(); })
      .then((rows: StationRide[]) => { setData(rows); setLoading(false); })
      .catch(e => { if (e.name !== "AbortError") { setError(e.message); setLoading(false); } });
    return () => ctrl.abort();
  }, [filters]);

  // Dismiss pinned popup when filters change (data refreshes = stale popup)
  useEffect(() => { setPinnedPopup(null); }, [filters]);

  // Push new bar geometry to the fill-extrusion source whenever the data, metric,
  // or scale changes. The source + layer + event handlers are created once on map
  // load (below); here we only swap the data.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    const src = map.getSource("rides") as mapboxgl.GeoJSONSource | undefined;
    if (src) src.setData(buildBarsGeoJSON(data, filters.metric, elevScale, boroughRankMap));
  }, [data, filters.metric, elevScale, boroughRankMap, mapLoaded]);

  // Init map once
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    mapboxgl.accessToken = process.env.NEXT_PUBLIC_MAPBOX_TOKEN!;
    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/dark-v11",
      center: [-73.95, 40.735],
      zoom: 11.6,
      pitch: 52,
      bearing: -17,
      minZoom: 9.5,
      maxZoom: 16,
    });
    map.addControl(new mapboxgl.NavigationControl(), "top-right");
    map.on("load", () => {
      // Native Mapbox fill-extrusion — the exact technique manpopex uses. Because the
      // bars are rendered by Mapbox's own engine they share the basemap's camera and
      // stay upright at any pitch/zoom, instead of leaning like a deck.gl overlay did.
      map.addSource("rides", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "rides-extrusion",
        type: "fill-extrusion",
        source: "rides",
        paint: {
          "fill-extrusion-color": ["get", "color"],
          "fill-extrusion-height": ["get", "height"],
          "fill-extrusion-base": 0,
          "fill-extrusion-opacity": 0.92,
          "fill-extrusion-height-transition": { duration: 300, delay: 0 },
        },
      });

      // Hover tooltip
      map.on("mousemove", "rides-extrusion", (e) => {
        if (!e.features?.length) return;
        map.getCanvas().style.cursor = "pointer";
        setTooltip({ x: e.point.x, y: e.point.y, d: propsToStation(e.features[0].properties as Record<string, unknown>) });
      });
      map.on("mouseleave", "rides-extrusion", () => {
        map.getCanvas().style.cursor = "";
        setTooltip(null);
      });

      // Click a bar -> pinned popup; click empty map -> dismiss it
      map.on("click", (e) => {
        if (!map.getLayer("rides-extrusion")) return;
        const feats = map.queryRenderedFeatures(e.point, { layers: ["rides-extrusion"] });
        if (!feats.length) { setPinnedPopup(null); return; }
        const p = feats[0].properties as Record<string, unknown>;
        setPinnedPopup({ x: e.point.x, y: e.point.y, d: propsToStation(p), rank: Number(p.rank) || 0 });
      });

      setMapLoaded(true);
    });
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      setMapLoaded(false);
    };
  }, []);

  const toggleBorough = useCallback((b: string) => {
    setFilters(f => {
      const next = new Set(f.boroughs);
      if (next.has(b)) next.delete(b); else next.add(b);
      return { ...f, boroughs: next };
    });
  }, []);

  const set = useCallback(<K extends keyof Filters>(k: K, v: Filters[K]) => {
    setFilters(f => ({ ...f, [k]: v }));
  }, []);

  return (
    <div className="relative flex-1" style={{ minHeight: 0 }}>
      {/* Map fills the entire container via absolute positioning so Mapbox
          always gets reliable non-zero dimensions at init time */}
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />

      {/* Filter panel */}
      <div className="absolute left-3 top-3 z-10 w-52 rounded-xl bg-black/80 backdrop-blur p-4 flex flex-col gap-3 overflow-y-auto max-h-[calc(100vh-7rem)] text-white">
        <div className="text-sm font-semibold text-white">Ride Explorer</div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-zinc-400 uppercase tracking-wide">Year</label>
          <select value={filters.year} onChange={e => set("year", e.target.value)} className={SELECT_CLS}>
            {YEARS.map(y => <option key={y} value={y}>{y}</option>)}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-zinc-400 uppercase tracking-wide">Month</label>
          <select value={filters.month} onChange={e => set("month", e.target.value)} className={SELECT_CLS}>
            {MONTHS.map(m => <option key={m.v} value={m.v}>{m.l}</option>)}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-zinc-400 uppercase tracking-wide">Day of Week</label>
          <select value={filters.dow} onChange={e => set("dow", e.target.value)} className={SELECT_CLS}>
            {DAYS.map(d => <option key={d.v} value={d.v}>{d.l}</option>)}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-zinc-400 uppercase tracking-wide">Time of Day</label>
          <select value={filters.hour} onChange={e => set("hour", e.target.value)} className={SELECT_CLS}>
            {HOURS.map(h => <option key={h.v} value={h.v}>{h.l}</option>)}
          </select>
        </div>

        <div className="border-t border-zinc-700 pt-2 flex flex-col gap-1">
          <label className="text-[11px] text-zinc-400 uppercase tracking-wide">Bike Type</label>
          {(["all", "ebike", "classic"] as const).map(v => (
            <label key={v} className={RADIO_LABEL_CLS}>
              <input type="radio" name="bikeType" value={v}
                checked={filters.bikeType === v}
                onChange={() => set("bikeType", v)}
                className="accent-blue-500"
              />
              {v === "all" ? "All bikes" : v === "ebike" ? "E-bike" : "Classic"}
            </label>
          ))}
        </div>

        <div className="border-t border-zinc-700 pt-2 flex flex-col gap-1">
          <label className="text-[11px] text-zinc-400 uppercase tracking-wide">Rider Type</label>
          {(["all", "member", "casual"] as const).map(v => (
            <label key={v} className={RADIO_LABEL_CLS}>
              <input type="radio" name="riderType" value={v}
                checked={filters.riderType === v}
                onChange={() => set("riderType", v)}
                className="accent-blue-500"
              />
              {v === "all" ? "All riders" : v === "member" ? "Member" : "Casual"}
            </label>
          ))}
        </div>

        <div className="border-t border-zinc-700 pt-2 flex flex-col gap-1">
          <label className="text-[11px] text-zinc-400 uppercase tracking-wide">Metric</label>
          <div className="flex rounded overflow-hidden border border-zinc-600">
            {(["avg", "total"] as const).map(v => (
              <button key={v}
                onClick={() => set("metric", v)}
                className={`flex-1 py-1 text-xs font-medium transition-colors ${
                  filters.metric === v ? "bg-blue-600 text-white" : "bg-zinc-800 text-zinc-300 hover:text-white"
                }`}
              >
                {v === "avg" ? "Avg Rides" : "Total Rides"}
              </button>
            ))}
          </div>
        </div>

        <div className="border-t border-zinc-700 pt-2 flex flex-col gap-1">
          <label className="text-[11px] text-zinc-400 uppercase tracking-wide">Borough</label>
          {ALL_BOROUGHS.map(b => (
            <label key={b} className={RADIO_LABEL_CLS}>
              <input type="checkbox" checked={filters.boroughs.has(b)}
                onChange={() => toggleBorough(b)} className="accent-blue-500"
              />
              {b}
            </label>
          ))}
        </div>
      </div>

      {/* Bottom legend */}
      <div className="absolute bottom-8 right-3 z-10 rounded-xl bg-black/80 backdrop-blur p-3 text-xs text-white">
        {LEGEND_COLORS.map(([name, [r, g, b]]) => (
          <div key={name} className="flex items-center gap-2 mb-1 last:mb-0">
            <span className="h-3 w-3 shrink-0 rounded-sm" style={{ background: `rgb(${r},${g},${b})` }} />
            {name}
          </div>
        ))}
        <div className="mt-2 pt-2 border-t border-zinc-600 text-zinc-400">
          Bar height = {filters.metric === "avg" ? "avg rides/hr" : "total rides"}
        </div>
      </div>

      {/* Hover tooltip — hidden when pinned popup is open for same station */}
      {tooltip && tooltip.d.station_id !== pinnedPopup?.d.station_id && (
        <div className="pointer-events-none absolute z-20 rounded bg-black/90 px-2 py-1.5 text-xs text-white"
          style={{ left: tooltip.x + 12, top: tooltip.y - 10 }}>
          <span className="font-semibold">{tooltip.d.station_name}</span>
          <span className="ml-2 text-zinc-300">
            {filters.metric === "avg"
              ? `${tooltip.d.avg_rides.toFixed(1)} avg rides/hr`
              : `${Math.round(tooltip.d.total_rides).toLocaleString()} total rides`}
          </span>
        </div>
      )}

      {/* Pinned click popup — renders at screen coords so it's never blocked by WebGL bars */}
      {pinnedPopup && (() => {
        const { x, y, d, rank } = pinnedPopup;
        const metricVal = filters.metric === "avg"
          ? `${d.avg_rides.toFixed(1)} avg rides/hr`
          : `${Math.round(d.total_rides).toLocaleString()} total rides`;
        const utilization = d.capacity > 0
          ? `${(d.avg_rides / d.capacity).toFixed(2)} rides/dock/hr`
          : null;
        // Clamp so popup stays inside the map area (popup is 220px wide, ~230px tall)
        const left = Math.min(Math.max(x - 110, 4), (containerRef.current?.offsetWidth ?? 800) - 224);
        const top  = Math.max(y - 270, 4);
        return (
          <div className="absolute z-30 w-56 rounded-xl bg-white shadow-2xl text-sm"
            style={{ left, top }}
            onClick={e => e.stopPropagation()}>
            {/* Header */}
            <div className="flex items-start justify-between gap-1 px-3 pt-3 pb-1">
              <div>
                <div className="font-bold text-gray-900 leading-tight">{d.station_name}</div>
                <div className="text-xs text-gray-500 mt-0.5">{d.borough} · {d.capacity} docks</div>
              </div>
              <button onClick={() => setPinnedPopup(null)}
                className="text-gray-400 hover:text-gray-700 mt-0.5 shrink-0 leading-none text-lg font-light">
                ×
              </button>
            </div>
            {/* Stats */}
            <div className="px-3 py-2 border-t border-gray-100 space-y-1.5">
              <div className="flex justify-between items-center">
                <span className="text-xs text-gray-500">
                  {filters.metric === "avg" ? "Avg rides" : "Total rides"}
                </span>
                <span className="text-xs font-semibold text-gray-900">{metricVal}</span>
              </div>
              {utilization && (
                <div className="flex justify-between items-center">
                  <span className="text-xs text-gray-500">Utilization</span>
                  <span className="text-xs font-semibold text-gray-900">{utilization}</span>
                </div>
              )}
              {rank > 0 && (
                <div className="flex justify-between items-center">
                  <span className="text-xs text-gray-500">Rank in {d.borough}</span>
                  <span className="text-xs font-semibold text-gray-900">#{rank}</span>
                </div>
              )}
            </div>
            {/* Actions */}
            <div className="px-3 pb-3 pt-1 flex flex-col gap-2">
              <a href={`/station/${d.station_id}`}
                className="block text-center py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-xs font-semibold">
                View forecast →
              </a>
              <a href={`/signup?station_id=${d.station_id}`}
                className="block text-center py-1.5 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg text-xs font-semibold">
                Get alerts for this station
              </a>
            </div>
          </div>
        );
      })()}

      {/* Loading overlay */}
      {loading && (
        <div className="absolute inset-0 z-20 flex items-center justify-center pointer-events-none">
          <span className="rounded-lg bg-black/70 px-4 py-2 text-sm text-white backdrop-blur">
            Loading…
          </span>
        </div>
      )}

      {error && (
        <div className="absolute left-1/2 top-4 z-20 -translate-x-1/2 rounded-lg bg-red-900/80 px-3 py-2 text-xs text-white">
          {error}
        </div>
      )}
    </div>
  );
}
