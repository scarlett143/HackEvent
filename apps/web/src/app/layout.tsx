import type { Metadata } from "next";
import { Archivo_Narrow, Geist } from "next/font/google";
import localFont from "next/font/local";
import { THEME_INIT_SCRIPT } from "@/lib/theme";
import "./globals.css";

const geist = Geist({ subsets: ["latin"], variable: "--font-geist" });
const archivo = Archivo_Narrow({ subsets: ["latin"], variable: "--font-archivo" });
// Material Symbols subset (101 glyphs, 15 KB) so icons cost no network and pass font-src 'self'.
const icons = localFont({ src: "./fonts/material-symbols.woff2", variable: "--font-icons", display: "block" });

export const metadata: Metadata = {
  title: "HackEvents",
  description: "Hackathon discovery and reminders",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geist.variable} ${archivo.variable} ${icons.variable} h-full font-sans antialiased`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
