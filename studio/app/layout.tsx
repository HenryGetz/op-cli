import type { Metadata } from "next";
import { Space_Mono } from "next/font/google";
import type { ReactNode } from "react";

import { TopNav } from "@/components/top-nav";

import "./globals.css";

const mono = Space_Mono({
  subsets: ["latin"],
  weight: ["400", "700"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Caliper Studio",
  description: "Author, triage, and version visual UI assertions",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className={`${mono.className} min-h-screen bg-slate-950 text-slate-100`}>
        <TopNav />
        <main className="mx-auto w-full max-w-[1600px] p-4">{children}</main>
      </body>
    </html>
  );
}
