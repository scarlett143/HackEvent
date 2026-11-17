import type { Metadata } from "next";
import { Archivo_Narrow, Geist } from "next/font/google";
import "./globals.css";

const geist = Geist({ subsets: ["latin"], variable: "--font-geist" });
const archivo = Archivo_Narrow({ subsets: ["latin"], variable: "--font-archivo" });

export const metadata: Metadata = {
  title: "HackEvents",
  description: "Hackathon discovery and reminders",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geist.variable} ${archivo.variable} h-full font-sans antialiased`}>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
