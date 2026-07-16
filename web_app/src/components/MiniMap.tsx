"use client";

import { useEffect, useRef } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

type MiniMapProps = {
  lat: number;
  lon: number;
  name: string;
};

export default function MiniMap({ lat, lon, name }: MiniMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<mapboxgl.Map | null>(null);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    mapboxgl.accessToken = process.env.NEXT_PUBLIC_MAPBOX_TOKEN!;

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/dark-v11",
      center: [lon, lat],
      zoom: 15,
      interactive: false,
    });

    new mapboxgl.Marker({ color: "#2563eb" })
      .setLngLat([lon, lat])
      .setPopup(new mapboxgl.Popup({ closeButton: false }).setText(name))
      .addTo(map);

    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, [lat, lon, name]);

  return (
    <div
      ref={containerRef}
      className="h-full w-full rounded-lg overflow-hidden"
    />
  );
}
