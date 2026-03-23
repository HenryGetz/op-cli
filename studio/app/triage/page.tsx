"use client";

import { useEffect, useMemo, useState } from "react";

import { dataUrlToFile, payloadError, postCaliperForm, toDataUrl } from "@/lib/api";
import {
  computeChangedAssertions,
  loadProjectHistory,
  saveProjectHistory,
  SELECTED_RECORD_KEY,
} from "@/lib/history";
import type { BBox, CaliperPayload, CheckResult, HistoryItem } from "@/lib/types";

type ConfigShape = {
  project_name?: string;
  regions?: Record<string, { x: number; y: number; w: number; h: number }>;
  assertions?: Array<Record<string, unknown>>;
};

type Overlay = {
  id: string;
  rect: BBox;
  passed: boolean;
  delta: number;
};

function asNumber(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return 0;
}

function assertionToBox(assertion: Record<string, unknown>, config: ConfigShape): BBox | null {
  const regions = config.regions ?? {};
  const assertionType = String(assertion.type ?? "");
  if (assertionType === "region_dimension" || assertionType === "elements_in_region" || assertionType === "region_color_dominant") {
    const regionName = String(assertion.region ?? "");
    const region = regions[regionName];
    if (!region) {
      return null;
    }
    return {
      x: asNumber(region.x),
      y: asNumber(region.y),
      width: asNumber(region.w),
      height: asNumber(region.h),
    };
  }

  if (assertionType !== "measurement") {
    return null;
  }

  const fromRef = String((assertion.from as { ref?: string } | undefined)?.ref ?? "");
  const toRef = String((assertion.to as { ref?: string } | undefined)?.ref ?? "");

  const fromName = fromRef.startsWith("region:") ? fromRef.slice("region:".length) : "";
  const toName = toRef.startsWith("region:") ? toRef.slice("region:".length) : "";
  const fromRegion = regions[fromName];
  const toRegion = regions[toName];
  if (!fromRegion && !toRegion) {
    return null;
  }
  if (fromRegion && !toRegion) {
    return {
      x: asNumber(fromRegion.x),
      y: asNumber(fromRegion.y),
      width: asNumber(fromRegion.w),
      height: asNumber(fromRegion.h),
    };
  }
  if (!fromRegion && toRegion) {
    return {
      x: asNumber(toRegion.x),
      y: asNumber(toRegion.y),
      width: asNumber(toRegion.w),
      height: asNumber(toRegion.h),
    };
  }

  const left = Math.min(asNumber(fromRegion?.x), asNumber(toRegion?.x));
  const top = Math.min(asNumber(fromRegion?.y), asNumber(toRegion?.y));
  const right = Math.max(asNumber(fromRegion?.x) + asNumber(fromRegion?.w), asNumber(toRegion?.x) + asNumber(toRegion?.w));
  const bottom = Math.max(asNumber(fromRegion?.y) + asNumber(fromRegion?.h), asNumber(toRegion?.y) + asNumber(toRegion?.h));
  return { x: left, y: top, width: right - left, height: bottom - top };
}

function fixHint(assertion: Record<string, unknown> | undefined, result: CheckResult): string {
  const delta = asNumber(result.delta);
  if (!assertion) {
    return "Inspect assertion details and align the UI to its target geometry.";
  }
  const type = String(assertion.type ?? "");

  if (type === "region_dimension") {
    const property = String(assertion.property ?? "size");
    if (delta > 0) {
      return `element is ${Math.abs(delta).toFixed(0)}px too ${property === "height" ? "tall" : "wide"} — reduce ${property} by ${Math.abs(delta).toFixed(0)}px`;
    }
    return `element is ${Math.abs(delta).toFixed(0)}px too ${property === "height" ? "short" : "narrow"} — increase ${property} by ${Math.abs(delta).toFixed(0)}px`;
  }

  if (type === "measurement") {
    const axis = String(assertion.axis ?? "distance");
    if (delta > 0) {
      return `${axis} gap is ${Math.abs(delta).toFixed(0)}px too large — tighten spacing by ${Math.abs(delta).toFixed(0)}px`;
    }
    return `${axis} gap is ${Math.abs(delta).toFixed(0)}px too small — add ${Math.abs(delta).toFixed(0)}px spacing`;
  }

  return "Adjust the affected area until the assertion matches expected values.";
}

function downloadReport(payload: CaliperPayload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `caliper-triage-${Date.now()}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function TriagePage() {
  const [referenceFile, setReferenceFile] = useState<File | null>(null);
  const [iterationFile, setIterationFile] = useState<File | null>(null);
  const [referenceUrl, setReferenceUrl] = useState("");
  const [iterationUrl, setIterationUrl] = useState("");
  const [configText, setConfigText] = useState("");
  const [config, setConfig] = useState<ConfigShape | null>(null);
  const [checkPayload, setCheckPayload] = useState<CaliperPayload | null>(null);
  const [diffPayload, setDiffPayload] = useState<CaliperPayload | null>(null);
  const [selectedFailureId, setSelectedFailureId] = useState("");
  const [lastSignature, setLastSignature] = useState("");
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const selectedRaw = window.localStorage.getItem(SELECTED_RECORD_KEY);
    if (!selectedRaw) {
      return;
    }
    try {
      const record = JSON.parse(selectedRaw) as HistoryItem;
      if (record.referenceDataUrl) {
        const file = dataUrlToFile(record.referenceDataUrl, "history-reference.png");
        setReferenceFile(file);
        setReferenceUrl(record.referenceDataUrl);
      }
      if (record.iterationDataUrl) {
        const file = dataUrlToFile(record.iterationDataUrl, "history-iteration.png");
        setIterationFile(file);
        setIterationUrl(record.iterationDataUrl);
      }
      if (record.configText) {
        setConfigText(record.configText);
        setConfig(JSON.parse(record.configText) as ConfigShape);
      }
      if (record.checkPayload) {
        setCheckPayload(record.checkPayload);
      }
    } catch {
      // ignore malformed selected record
    } finally {
      window.localStorage.removeItem(SELECTED_RECORD_KEY);
    }
  }, []);

  const overlays = useMemo(() => {
    if (!checkPayload || !config || !Array.isArray(checkPayload.results)) {
      return [] as Overlay[];
    }

    const assertionById = new Map<string, Record<string, unknown>>(
      (config.assertions ?? []).map((assertion) => [String(assertion.id ?? ""), assertion]),
    );

    const items: Overlay[] = [];
    for (const result of checkPayload.results as CheckResult[]) {
      const assertion = assertionById.get(result.id);
      if (!assertion) {
        continue;
      }
      const rect = assertionToBox(assertion, config);
      if (!rect) {
        continue;
      }
      items.push({
        id: result.id,
        rect,
        passed: result.passed === true,
        delta: asNumber(result.delta),
      });
    }
    return items;
  }, [checkPayload, config]);

  const failedResults = useMemo(() => {
    if (!checkPayload || !Array.isArray(checkPayload.results)) {
      return [] as CheckResult[];
    }
    return [...(checkPayload.results as CheckResult[])]
      .filter((result) => result.passed === false)
      .sort((a, b) => Math.abs(asNumber(b.delta)) - Math.abs(asNumber(a.delta)));
  }, [checkPayload]);

  const assertionById = useMemo(() => {
    const map = new Map<string, Record<string, unknown>>();
    for (const assertion of config?.assertions ?? []) {
      map.set(String(assertion.id ?? ""), assertion);
    }
    return map;
  }, [config]);

  const selectedFailure = failedResults.find((result) => result.id === selectedFailureId) ?? failedResults[0] ?? null;

  useEffect(() => {
    if (!selectedFailure && selectedFailureId) {
      setSelectedFailureId("");
    }
  }, [selectedFailure, selectedFailureId]);

  useEffect(() => {
    const signature = [
      referenceFile?.name,
      referenceFile?.size,
      referenceFile?.lastModified,
      iterationFile?.name,
      iterationFile?.size,
      iterationFile?.lastModified,
      configText.length,
    ].join(":");

    if (!referenceFile || !iterationFile || !configText || signature === lastSignature) {
      return;
    }

    setLastSignature(signature);
    void runTriage(referenceFile, iterationFile, configText);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [referenceFile, iterationFile, configText]);

  async function runTriage(reference: File, iteration: File, cfgText: string) {
    setIsBusy(true);
    setError("");
    try {
      const diffData = new FormData();
      diffData.append("image1", reference);
      diffData.append("image2", iteration);

      const checkData = new FormData();
      checkData.append("image", iteration);
      checkData.append("config_json", cfgText);

      const [diff, check] = await Promise.all([
        postCaliperForm("/diff", diffData),
        postCaliperForm("/check", checkData),
      ]);

      if (diff.status === "error") {
        throw new Error(payloadError(diff));
      }
      if (check.status === "error") {
        throw new Error(payloadError(check));
      }

      setDiffPayload(diff);
      setCheckPayload(check);

      const parsedConfig = JSON.parse(cfgText) as ConfigShape;
      const projectName = parsedConfig.project_name?.trim() || "default";
      const previous = loadProjectHistory(projectName);
      const previousResults = (previous[0]?.checkPayload.results as CheckResult[] | undefined) ?? [];
      const currentResults = (check.results as CheckResult[] | undefined) ?? [];
      const changedAssertionIds = computeChangedAssertions(previousResults, currentResults);

      const thumbnailDataUrl = await toDataUrl(iteration);
      const referenceDataUrl = await toDataUrl(reference);

      const nextItem: HistoryItem = {
        id: `history-${Date.now()}`,
        projectName,
        timestamp: new Date().toISOString(),
        thumbnailDataUrl,
        passCount: Number(check.summary?.passed ?? 0),
        failCount: Number(check.summary?.failed ?? 0),
        changedAssertionIds,
        checkPayload: check,
        referenceDataUrl,
        iterationDataUrl: thumbnailDataUrl,
        configText: cfgText,
      };
      saveProjectHistory(projectName, [nextItem, ...previous].slice(0, 50));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Triage failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="grid gap-4 xl:grid-cols-[1fr_1fr_320px]">
      <article className="panel p-4">
        <header className="mb-3">
          <h1 className="text-lg font-bold">Reference</h1>
          <p className="muted text-sm">Upload baseline screenshot.</p>
        </header>

        <label className="mb-3 inline-flex cursor-pointer rounded-sm border border-white/20 bg-white/5 px-3 py-2 text-sm hover:bg-white/10">
          Select Reference
          <input
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (!file) {
                return;
              }
              setReferenceFile(file);
              setReferenceUrl(URL.createObjectURL(file));
            }}
          />
        </label>

        <div className="relative min-h-[420px] rounded-sm border border-white/10 bg-black/25 p-2">
          {referenceUrl ? (
            <img src={referenceUrl} alt="Reference" className="max-h-[620px] w-full object-contain" />
          ) : (
            <p className="muted p-8 text-sm">No reference screenshot loaded.</p>
          )}
        </div>
      </article>

      <article className="panel p-4">
        <header className="mb-3 flex items-center justify-between">
          <div>
            <h1 className="text-lg font-bold">Iteration</h1>
            <p className="muted text-sm">Upload candidate screenshot + config.</p>
          </div>
          {checkPayload && (
            <button
              type="button"
              className="rounded-sm border border-white/30 bg-white/10 px-3 py-2 text-xs hover:bg-white/20"
              onClick={() => downloadReport(checkPayload)}
            >
              Export Report
            </button>
          )}
        </header>

        <div className="mb-3 flex flex-wrap gap-2">
          <label className="cursor-pointer rounded-sm border border-white/20 bg-white/5 px-3 py-2 text-sm hover:bg-white/10">
            Select Iteration
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (!file) {
                  return;
                }
                setIterationFile(file);
                setIterationUrl(URL.createObjectURL(file));
              }}
            />
          </label>

          <label className="cursor-pointer rounded-sm border border-white/20 bg-white/5 px-3 py-2 text-sm hover:bg-white/10">
            Load .caliper.json
            <input
              type="file"
              accept="application/json"
              className="hidden"
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (!file) {
                  return;
                }
                const text = await file.text();
                setConfigText(text);
                setConfig(JSON.parse(text) as ConfigShape);
              }}
            />
          </label>
        </div>

        <div className="relative min-h-[420px] rounded-sm border border-white/10 bg-black/25 p-2">
          {iterationUrl ? (
            <div className="relative">
              <img src={iterationUrl} alt="Iteration" className="max-h-[620px] w-full object-contain" />

              {overlays.map((overlay) => (
                <button
                  key={overlay.id}
                  type="button"
                  onClick={() => setSelectedFailureId(overlay.id)}
                  className="absolute border-2"
                  title={overlay.id}
                  style={{
                    left: `${(overlay.rect.x / (checkPayload?.meta?.image_width as number | undefined ?? 1)) * 100}%`,
                    top: `${(overlay.rect.y / (checkPayload?.meta?.image_height as number | undefined ?? 1)) * 100}%`,
                    width: `${(overlay.rect.width / (checkPayload?.meta?.image_width as number | undefined ?? 1)) * 100}%`,
                    height: `${(overlay.rect.height / (checkPayload?.meta?.image_height as number | undefined ?? 1)) * 100}%`,
                    borderColor: overlay.passed ? "#63f58a" : "#ff5d6c",
                    background: overlay.passed ? "rgba(99, 245, 138, 0.10)" : "rgba(255, 93, 108, 0.12)",
                  }}
                >
                  {!overlay.passed && (
                    <span className="absolute -top-5 left-0 rounded-sm bg-warn px-1 py-[1px] text-[10px] font-bold text-black">
                      {overlay.delta > 0 ? "+" : ""}
                      {overlay.delta.toFixed(0)}px
                    </span>
                  )}
                </button>
              ))}
            </div>
          ) : (
            <p className="muted p-8 text-sm">No iteration screenshot loaded.</p>
          )}
        </div>

        {isBusy && <p className="mt-3 text-sm text-accent">Running /diff + /check…</p>}
        {error && <p className="mt-3 text-sm text-warn">{error}</p>}
      </article>

      <aside className="panel p-4">
        <h2 className="text-sm font-bold uppercase tracking-[0.16em] text-slate-300">Failure Queue</h2>
        <p className="muted mt-2 text-xs">Sorted by absolute delta (worst first).</p>

        <div className="mt-3 max-h-[280px] space-y-2 overflow-auto">
          {failedResults.length === 0 && <p className="muted text-sm">No failures yet.</p>}
          {failedResults.map((result) => (
            <button
              key={result.id}
              type="button"
              className={`w-full rounded-sm border px-2 py-2 text-left text-xs transition ${
                selectedFailure?.id === result.id ? "border-warn bg-warn/10" : "border-white/15 bg-black/25"
              }`}
              onClick={() => setSelectedFailureId(result.id)}
            >
              <div className="font-semibold text-slate-100">{result.id}</div>
              <div className="mt-1 text-warn">
                {asNumber(result.delta) > 0 ? "+" : ""}
                {asNumber(result.delta).toFixed(0)}px
              </div>
            </button>
          ))}
        </div>

        {selectedFailure && (
          <div className="mt-4 rounded-sm border border-white/15 bg-black/25 p-3 text-xs">
            <h3 className="text-sm font-bold text-slate-100">{selectedFailure.id}</h3>
            <div className="mt-2 space-y-1 text-slate-300">
              <div>expected: {asNumber(selectedFailure.expected).toFixed(2)}</div>
              <div>actual: {asNumber(selectedFailure.actual).toFixed(2)}</div>
              <div>
                delta: {asNumber(selectedFailure.delta) > 0 ? "+" : ""}
                {asNumber(selectedFailure.delta).toFixed(2)}
              </div>
            </div>
            <p className="mt-3 rounded-sm border border-white/10 bg-black/30 p-2 text-[11px] text-slate-100">
              {fixHint(assertionById.get(selectedFailure.id), selectedFailure)}
            </p>
          </div>
        )}

        {diffPayload && (
          <div className="mt-4 rounded-sm border border-white/10 bg-black/20 p-3 text-xs text-slate-300">
            <div>similarity: {String(diffPayload.diff?.similarity_score ?? "-")}</div>
            <div>added: {String(diffPayload.diff?.added_elements?.length ?? 0)}</div>
            <div>removed: {String(diffPayload.diff?.removed_elements?.length ?? 0)}</div>
          </div>
        )}
      </aside>
    </section>
  );
}
