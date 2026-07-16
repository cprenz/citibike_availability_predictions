import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Script from "next/script";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Citi Bike Availability Predictions",
  description:
    "Predicted Citi Bike availability across NYC stations, 1 hour to multiple days ahead.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <nav className="flex items-center gap-6 border-b border-black/10 px-6 py-4 text-sm font-medium dark:border-white/10">
          <a href="/" className="font-semibold">
            Citi Bike Predictions
          </a>
          <a href="/" className="text-zinc-600 hover:text-black dark:text-zinc-400 dark:hover:text-white">
            Map
          </a>
          <a href="/dashboard" className="text-zinc-600 hover:text-black dark:text-zinc-400 dark:hover:text-white">
            Dashboard
          </a>
          <a href="/signup" className="text-zinc-600 hover:text-black dark:text-zinc-400 dark:hover:text-white">
            Get Alerts
          </a>
        </nav>
        <div className="flex flex-1 flex-col">{children}</div>

        {/* Google Analytics 4 */}
        <Script
          src="https://www.googletagmanager.com/gtag/js?id=G-ZW0S7TE9CW"
          strategy="afterInteractive"
        />
        <Script id="ga4-init" strategy="afterInteractive">
          {`
            window.dataLayer = window.dataLayer || [];
            function gtag(){dataLayer.push(arguments);}
            gtag('js', new Date());
            gtag('config', 'G-ZW0S7TE9CW');
          `}
        </Script>

        {/* Contentsquare (Hotjar) */}
        <Script
          src="https://t.contentsquare.net/uxa/d646b9388a72e.js"
          strategy="afterInteractive"
        />
      </body>
    </html>
  );
}
