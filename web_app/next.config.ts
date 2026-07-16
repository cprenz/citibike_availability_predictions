import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  serverExternalPackages: ["pg", "pg-native", "snowflake-sdk"],
  typescript: {
    ignoreBuildErrors: true,
  },
};

export default nextConfig;
