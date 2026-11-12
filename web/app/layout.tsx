import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Prop Research",
  description: "Player prop research: lines, Kalshi strikes and hit rates.",
  applicationName: "Prop Research",
  appleWebApp: { capable: true, title: "Props", statusBarStyle: "black-translucent" },
  robots: { index: false, follow: false }, // personal, login-only app: never indexed
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f6f7f9" },
    { media: "(prefers-color-scheme: dark)", color: "#0f1115" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
