import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  serverExternalPackages: ["pg", "pg-native", "@google-cloud/bigquery"],
  transpilePackages: ["@deck.gl/core", "@deck.gl/layers", "@deck.gl/mapbox"],
  typescript: {
    ignoreBuildErrors: true,
  },
};

export default nextConfig;
