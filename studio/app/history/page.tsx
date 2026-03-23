"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { AUTHOR_SEED_KEY, historyKey, listHistoryProjects, SELECTED_RECORD_KEY } from "@/lib/history";
import type { HistoryItem } from "@/lib/types";

type ProjectHistory = {
  projectName: string;
  items: HistoryItem[];
};

function relativeTime(iso: string): string {
  const delta = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(delta / 60000);
  if (minutes < 1) {
    return "just now";
  }
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

export default function HistoryPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectHistory[]>([]);

  useEffect(() => {
    const loadedProjects: ProjectHistory[] = [];
    for (const projectName of listHistoryProjects()) {
      const raw = window.localStorage.getItem(historyKey(projectName));
      if (!raw) {
        continue;
      }
      try {
        const items = JSON.parse(raw) as HistoryItem[];
        loadedProjects.push({
          projectName,
          items: Array.isArray(items) ? items : [],
        });
      } catch {
        // ignore broken JSON entry
      }
    }
    loadedProjects.sort((a, b) => {
      const aTime = new Date(a.items[0]?.timestamp ?? 0).getTime();
      const bTime = new Date(b.items[0]?.timestamp ?? 0).getTime();
      return bTime - aTime;
    });
    setProjects(loadedProjects);
  }, []);

  const totalRows = useMemo(() => projects.reduce((sum, project) => sum + project.items.length, 0), [projects]);

  return (
    <section className="panel p-4">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold">History Mode</h1>
          <p className="muted text-sm">Local iteration timeline keyed by project name from `.caliper.json`.</p>
        </div>
        <div className="rounded-sm border border-white/15 bg-black/25 px-3 py-2 text-xs text-slate-300">{totalRows} iterations tracked</div>
      </header>

      {projects.length === 0 ? (
        <p className="muted py-6 text-sm">No history yet. Run Triage at least once to populate this page.</p>
      ) : (
        <div className="space-y-6">
          {projects.map((project) => (
            <div key={project.projectName}>
              <h2 className="mb-2 text-sm font-bold uppercase tracking-[0.16em] text-slate-300">{project.projectName}</h2>
              <div className="space-y-2">
                {project.items.map((item) => (
                  <div key={item.id} className="grid grid-cols-[100px_1fr_auto] items-center gap-3 rounded-sm border border-white/10 bg-black/20 p-2">
                    <img src={item.thumbnailDataUrl} alt="Iteration thumbnail" className="h-16 w-full rounded-sm object-cover" />

                    <button
                      type="button"
                      className="text-left"
                      onClick={() => {
                        window.localStorage.setItem(SELECTED_RECORD_KEY, JSON.stringify(item));
                        router.push("/triage");
                      }}
                    >
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="rounded-sm border border-pass/60 bg-pass/10 px-1 py-[2px] text-pass">pass {item.passCount}</span>
                        <span className="rounded-sm border border-warn/60 bg-warn/10 px-1 py-[2px] text-warn">fail {item.failCount}</span>
                        <span className="muted text-xs">{relativeTime(item.timestamp)}</span>
                      </div>
                      <p className="muted mt-1 text-xs">
                        changed assertions: {item.changedAssertionIds.length > 0 ? item.changedAssertionIds.join(", ") : "none"}
                      </p>
                    </button>

                    <button
                      type="button"
                      className="rounded-sm border border-accent/70 bg-accent/15 px-3 py-2 text-xs text-accent hover:bg-accent/25"
                      onClick={() => {
                        window.localStorage.setItem(
                          AUTHOR_SEED_KEY,
                          JSON.stringify({
                            imageDataUrl: item.iterationDataUrl ?? item.thumbnailDataUrl,
                            projectName: item.projectName,
                          }),
                        );
                        router.push("/author");
                      }}
                    >
                      Pin as baseline
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
