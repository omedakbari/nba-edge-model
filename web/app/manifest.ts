import type { MetadataRoute } from "next";

// Installable PWA (Add to Home Screen). No service worker yet on purpose: caching stale prices is
// worse than showing none, so offline support waits until freshness gates exist (Phase 5).
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Prop Research",
    short_name: "Props",
    description: "Player prop research: lines, Kalshi strikes and hit rates.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    orientation: "portrait",
    background_color: "#0f1115",
    theme_color: "#0f1115",
    icons: [
      { src: "/icon", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/apple-icon", sizes: "180x180", type: "image/png" },
    ],
  };
}
