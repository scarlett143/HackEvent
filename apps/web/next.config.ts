import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone", // Docker image runs .next/standalone (HE-002)
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
