"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/author", label: "Author" },
  { href: "/triage", label: "Triage" },
  { href: "/history", label: "History" },
];

export function TopNav() {
  const pathname = usePathname();

  return (
    <nav className="sticky top-0 z-20 border-b border-white/10 bg-panelAlt/90 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1600px] items-center justify-between px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="rounded-sm border border-accent/40 bg-accent/10 px-2 py-1 text-xs font-bold uppercase tracking-[0.18em] text-accent">
            Caliper Studio
          </div>
          <span className="text-xs text-slate-400">Visual QA Workbench</span>
        </div>
        <div className="flex items-center gap-2">
          {links.map((link) => {
            const active = pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`rounded-sm px-3 py-2 text-sm transition ${
                  active
                    ? "bg-accent text-panelAlt shadow-[0_0_18px_rgba(57,213,182,0.35)]"
                    : "bg-white/5 text-slate-200 hover:bg-white/10"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </div>
      </div>
    </nav>
  );
}
