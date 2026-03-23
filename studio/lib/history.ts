import type { CheckResult, HistoryItem } from "@/lib/types";

const PREFIX = "caliperui.history.";
export const SELECTED_RECORD_KEY = "caliperui.selectedRecord";
export const AUTHOR_SEED_KEY = "caliperui.authorSeed";

export function historyKey(projectName: string): string {
  return `${PREFIX}${projectName.trim() || "default"}`;
}

export function loadProjectHistory(projectName: string): HistoryItem[] {
  if (typeof window === "undefined") {
    return [];
  }
  const raw = window.localStorage.getItem(historyKey(projectName));
  if (!raw) {
    return [];
  }
  try {
    const parsed = JSON.parse(raw) as HistoryItem[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function saveProjectHistory(projectName: string, items: HistoryItem[]): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(historyKey(projectName), JSON.stringify(items));
}

export function listHistoryProjects(): string[] {
  if (typeof window === "undefined") {
    return [];
  }
  const names: string[] = [];
  for (let i = 0; i < window.localStorage.length; i += 1) {
    const key = window.localStorage.key(i);
    if (!key || !key.startsWith(PREFIX)) {
      continue;
    }
    names.push(key.slice(PREFIX.length));
  }
  return names.sort((a, b) => a.localeCompare(b));
}

export function computeChangedAssertions(previous: CheckResult[], current: CheckResult[]): string[] {
  const prev = new Map(previous.map((item) => [item.id, item.passed]));
  const changed: string[] = [];
  for (const item of current) {
    if (prev.get(item.id) !== item.passed) {
      changed.push(item.id);
    }
  }
  return changed;
}
