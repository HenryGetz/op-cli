"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";

import { dataUrlToFile, payloadError, postCaliperForm, toDataUrl } from "@/lib/api";
import { AUTHOR_SEED_KEY } from "@/lib/history";
import type { BBox, CaliperPayload, ParsedElement } from "@/lib/types";

type Rect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

type CustomRegion = {
  id: string;
  name: string;
  tolerance: number;
  assertionType: "region_dimension" | "measurement";
  rect: Rect;
};

type OverlayRegion = {
  id: string;
  label: string;
  rect: Rect;
  color: string;
};

type ElementOption = {
  tolerance: number;
  assertionType: "region_dimension" | "measurement";
};

const DEFAULT_TOLERANCE = 5;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function ensureRect(a: { x: number; y: number }, b: { x: number; y: number }): Rect {
  const left = Math.min(a.x, b.x);
  const top = Math.min(a.y, b.y);
  return {
    x: left,
    y: top,
    width: Math.abs(a.x - b.x),
    height: Math.abs(a.y - b.y),
  };
}

function downloadJson(fileName: string, payload: unknown) {
  const json = JSON.stringify(payload, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function AuthorPage() {
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [imageUrl, setImageUrl] = useState<string>("");
  const [imageSize, setImageSize] = useState<{ width: number; height: number } | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState("");
  const [elements, setElements] = useState<ParsedElement[]>([]);
  const [selectedElementKey, setSelectedElementKey] = useState<string>("");
  const [elementOptions, setElementOptions] = useState<Record<string, ElementOption>>({});
  const [drawMode, setDrawMode] = useState(false);
  const [drawingStart, setDrawingStart] = useState<{ x: number; y: number } | null>(null);
  const [draftRect, setDraftRect] = useState<Rect | null>(null);
  const [customRegions, setCustomRegions] = useState<CustomRegion[]>([]);
  const [dryRunRegions, setDryRunRegions] = useState<OverlayRegion[]>([]);
  const [projectName, setProjectName] = useState("caliper-project");

  useEffect(() => {
    const seedRaw = window.localStorage.getItem(AUTHOR_SEED_KEY);
    if (!seedRaw) {
      return;
    }
    try {
      const seed = JSON.parse(seedRaw) as { imageDataUrl?: string; projectName?: string };
      if (seed.projectName) {
        setProjectName(seed.projectName);
      }
      if (seed.imageDataUrl) {
        const seededFile = dataUrlToFile(seed.imageDataUrl, "seeded-baseline.png");
        void handleImageSelected(seededFile);
      }
    } catch {
      // ignore malformed localStorage seed
    } finally {
      window.localStorage.removeItem(AUTHOR_SEED_KEY);
    }
  }, []);

  useEffect(() => {
    return () => {
      if (imageUrl.startsWith("blob:")) {
        URL.revokeObjectURL(imageUrl);
      }
    };
  }, [imageUrl]);

  const selectedElement = useMemo(() => {
    if (!selectedElementKey) {
      return null;
    }
    return elements.find((element) => (element.element_id ?? `idx-${element.index}`) === selectedElementKey) ?? null;
  }, [elements, selectedElementKey]);

  const overlaysFromElements = useMemo(() => {
    return elements.map((element, index) => {
      const key = element.element_id ?? `idx-${element.index}`;
      const hue = (index * 47) % 360;
      return {
        id: key,
        label: element.label || `element:${element.index}`,
        color: `hsl(${hue} 90% 60%)`,
        rect: element.bbox,
      };
    });
  }, [elements]);

  const allDryRunRegions = useMemo(() => [...dryRunRegions, ...customRegions.map((region) => ({
    id: region.id,
    label: region.name,
    rect: region.rect,
    color: "#ffe08a",
  }))], [customRegions, dryRunRegions]);

  const getPointerInImage = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      if (!canvasRef.current || !imageSize) {
        return null;
      }
      const bounds = canvasRef.current.getBoundingClientRect();
      const normalizedX = clamp((event.clientX - bounds.left) / bounds.width, 0, 1);
      const normalizedY = clamp((event.clientY - bounds.top) / bounds.height, 0, 1);
      return {
        x: normalizedX * imageSize.width,
        y: normalizedY * imageSize.height,
      };
    },
    [imageSize],
  );

  async function runParse(file: File) {
    const formData = new FormData();
    formData.append("image", file);
    const payload = await postCaliperForm("/parse", formData);
    if (payload.status === "error") {
      throw new Error(payloadError(payload));
    }

    const width = Number(payload.image_width ?? payload.meta?.image_width ?? 0);
    const height = Number(payload.image_height ?? payload.meta?.image_height ?? 0);
    if (width > 0 && height > 0) {
      setImageSize({ width, height });
    }

    const parsedElements = Array.isArray(payload.elements) ? (payload.elements as ParsedElement[]) : [];
    setElements(parsedElements);
    const optionSeed: Record<string, ElementOption> = {};
    for (const element of parsedElements) {
      const key = element.element_id ?? `idx-${element.index}`;
      optionSeed[key] = {
        tolerance: DEFAULT_TOLERANCE,
        assertionType: "region_dimension",
      };
    }
    setElementOptions(optionSeed);
    if (parsedElements.length > 0) {
      setSelectedElementKey(parsedElements[0].element_id ?? `idx-${parsedElements[0].index}`);
    }
  }

  async function handleImageSelected(file: File) {
    setIsBusy(true);
    setError("");
    setDryRunRegions([]);
    const objectUrl = URL.createObjectURL(file);
    setImageFile(file);
    setImageUrl(objectUrl);
    try {
      await runParse(file);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to parse image");
      setElements([]);
    } finally {
      setIsBusy(false);
    }
  }

  async function onGenerateConfig() {
    if (!imageFile) {
      setError("Upload a reference screenshot first.");
      return;
    }

    setIsBusy(true);
    setError("");
    try {
      const formData = new FormData();
      formData.append("image", imageFile);
      formData.append("project_name", projectName);
      formData.append("tolerance", String(DEFAULT_TOLERANCE));
      formData.append(
        "custom_regions",
        JSON.stringify(
          customRegions.map((region) => ({
            name: region.name,
            tolerance: region.tolerance,
            assertion_type: region.assertionType,
            rect: region.rect,
          })),
        ),
      );

      const payload = await postCaliperForm("/baseline", formData);
      if (payload.status === "error") {
        throw new Error(payloadError(payload));
      }

      const config = payload.baseline?.config;
      if (!config || typeof config !== "object") {
        throw new Error("Baseline payload did not include config JSON");
      }
      downloadJson(`${projectName || "baseline"}.caliper.json`, config);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate baseline config");
    } finally {
      setIsBusy(false);
    }
  }

  async function onDryRunUpload(file: File) {
    const text = await file.text();
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(text) as Record<string, unknown>;
    } catch {
      setError("Dry Run config must be valid JSON.");
      return;
    }

    const regions = (parsed.regions ?? {}) as Record<string, { x: number; y: number; w: number; h: number }>;
    const overlays: OverlayRegion[] = Object.entries(regions).map(([name, region], index) => ({
      id: `dry-${name}`,
      label: name,
      rect: {
        x: Number(region.x ?? 0),
        y: Number(region.y ?? 0),
        width: Number(region.w ?? 0),
        height: Number(region.h ?? 0),
      },
      color: index % 2 === 0 ? "#5ec4ff" : "#ffb46a",
    }));
    setDryRunRegions(overlays);
    if (typeof parsed.project_name === "string" && parsed.project_name.trim()) {
      setProjectName(parsed.project_name);
    }
  }

  return (
    <section className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <div className="panel p-4">
        <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-bold">Authoring Mode</h1>
            <p className="muted text-sm">Drop a reference screenshot, inspect detected boxes, draw regions, and export baseline config.</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setDrawMode((value) => !value)}
              className={`rounded-sm border px-3 py-2 text-sm ${drawMode ? "border-accent bg-accent/20 text-accent" : "border-white/20 bg-white/5"}`}
            >
              {drawMode ? "Drawing On" : "Draw Region"}
            </button>
            <button
              type="button"
              onClick={onGenerateConfig}
              className="rounded-sm border border-accent bg-accent/20 px-3 py-2 text-sm text-accent disabled:opacity-50"
              disabled={!imageFile || isBusy}
            >
              Generate Config
            </button>
          </div>
        </header>

        <div
          className="relative min-h-[420px] rounded-sm border border-dashed border-white/20 bg-black/25"
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            const file = event.dataTransfer.files[0];
            if (file) {
              void handleImageSelected(file);
            }
          }}
        >
          {!imageUrl ? (
            <div className="flex h-[420px] flex-col items-center justify-center gap-3 text-center">
              <p className="text-lg">Drop screenshot here</p>
              <p className="muted text-sm">or choose a file manually</p>
              <label className="cursor-pointer rounded-sm border border-white/25 bg-white/10 px-3 py-2 text-sm hover:bg-white/20">
                Select image
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) {
                      void handleImageSelected(file);
                    }
                  }}
                />
              </label>
            </div>
          ) : (
            <div
              ref={canvasRef}
              className="relative mx-auto h-full w-full overflow-hidden"
              onMouseDown={(event) => {
                if (!drawMode) {
                  return;
                }
                const point = getPointerInImage(event);
                if (!point) {
                  return;
                }
                setDrawingStart(point);
                setDraftRect({ x: point.x, y: point.y, width: 0, height: 0 });
              }}
              onMouseMove={(event) => {
                if (!drawMode || !drawingStart) {
                  return;
                }
                const point = getPointerInImage(event);
                if (!point) {
                  return;
                }
                setDraftRect(ensureRect(drawingStart, point));
              }}
              onMouseUp={(event) => {
                if (!drawMode || !drawingStart) {
                  return;
                }
                const point = getPointerInImage(event);
                setDrawingStart(null);
                if (!point) {
                  setDraftRect(null);
                  return;
                }
                const rect = ensureRect(drawingStart, point);
                setDraftRect(null);
                if (rect.width < 4 || rect.height < 4) {
                  return;
                }
                const id = `region-${Date.now()}-${Math.floor(Math.random() * 9999)}`;
                setCustomRegions((regions) => [
                  ...regions,
                  {
                    id,
                    name: `custom-${regions.length + 1}`,
                    tolerance: DEFAULT_TOLERANCE,
                    assertionType: "region_dimension",
                    rect,
                  },
                ]);
              }}
            >
              <img src={imageUrl} alt="Reference screenshot" className="max-h-[680px] w-full object-contain" />

              {imageSize && (
                <>
                  {overlaysFromElements.map((overlay) => {
                    const selected = overlay.id === selectedElementKey;
                    return (
                      <button
                        key={overlay.id}
                        type="button"
                        title={overlay.label}
                        className={`absolute border text-left transition ${selected ? "z-20 border-white shadow-[0_0_0_2px_rgba(255,255,255,0.55)]" : "z-10 border-white/80"}`}
                        style={{
                          left: `${(overlay.rect.x / imageSize.width) * 100}%`,
                          top: `${(overlay.rect.y / imageSize.height) * 100}%`,
                          width: `${(overlay.rect.width / imageSize.width) * 100}%`,
                          height: `${(overlay.rect.height / imageSize.height) * 100}%`,
                          background: `${overlay.color}22`,
                          borderColor: selected ? "#ffffff" : overlay.color,
                        }}
                        onClick={(event) => {
                          event.stopPropagation();
                          setSelectedElementKey(overlay.id);
                        }}
                      />
                    );
                  })}

                  {allDryRunRegions.map((overlay) => (
                    <div
                      key={overlay.id}
                      className="absolute border-2 border-dashed"
                      style={{
                        left: `${(overlay.rect.x / imageSize.width) * 100}%`,
                        top: `${(overlay.rect.y / imageSize.height) * 100}%`,
                        width: `${(overlay.rect.width / imageSize.width) * 100}%`,
                        height: `${(overlay.rect.height / imageSize.height) * 100}%`,
                        borderColor: overlay.color,
                        background: `${overlay.color}18`,
                      }}
                      title={overlay.label}
                    />
                  ))}

                  {draftRect && (
                    <div
                      className="absolute border-2 border-dashed border-accent bg-accent/20"
                      style={{
                        left: `${(draftRect.x / imageSize.width) * 100}%`,
                        top: `${(draftRect.y / imageSize.height) * 100}%`,
                        width: `${(draftRect.width / imageSize.width) * 100}%`,
                        height: `${(draftRect.height / imageSize.height) * 100}%`,
                      }}
                    />
                  )}
                </>
              )}
            </div>
          )}
        </div>

        <footer className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <label className="rounded-sm border border-white/20 bg-white/5 px-3 py-2 text-sm hover:bg-white/10">
            Dry Run (.caliper.json)
            <input
              type="file"
              accept="application/json"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) {
                  void onDryRunUpload(file);
                }
              }}
            />
          </label>

          <div className="flex items-center gap-2 text-sm">
            <span className="muted">Project:</span>
            <input
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
              className="rounded-sm border border-white/20 bg-black/30 px-2 py-1"
            />
          </div>
        </footer>

        {isBusy && <p className="mt-3 text-sm text-accent">Processing…</p>}
        {error && <p className="mt-3 text-sm text-warn">{error}</p>}
      </div>

      <aside className="panel p-4">
        <h2 className="text-sm font-bold uppercase tracking-[0.16em] text-slate-300">Inspector</h2>

        {selectedElement && imageSize ? (
          <div className="mt-3 space-y-3 text-sm">
            <div>
              <p className="text-xs uppercase tracking-[0.14em] text-slate-400">Element</p>
              <p className="mt-1 text-base">{selectedElement.label || `element:${selectedElement.index}`}</p>
            </div>
            <div className="rounded-sm border border-white/10 bg-black/25 p-3 text-xs leading-6">
              <div>x: {selectedElement.bbox.x.toFixed(0)}</div>
              <div>y: {selectedElement.bbox.y.toFixed(0)}</div>
              <div>w: {selectedElement.bbox.width.toFixed(0)}</div>
              <div>h: {selectedElement.bbox.height.toFixed(0)}</div>
            </div>

            <label className="block text-xs uppercase tracking-[0.14em] text-slate-400">
              Tolerance ({elementOptions[selectedElementKey]?.tolerance ?? DEFAULT_TOLERANCE}px)
              <input
                type="range"
                min={0}
                max={40}
                value={elementOptions[selectedElementKey]?.tolerance ?? DEFAULT_TOLERANCE}
                className="mt-2 w-full"
                onChange={(event) => {
                  const value = Number(event.target.value);
                  setElementOptions((state) => ({
                    ...state,
                    [selectedElementKey]: {
                      ...state[selectedElementKey],
                      tolerance: value,
                    },
                  }));
                }}
              />
            </label>

            <label className="block text-xs uppercase tracking-[0.14em] text-slate-400">
              Assertion Type
              <select
                className="mt-2 w-full rounded-sm border border-white/20 bg-black/30 px-2 py-2 text-sm"
                value={elementOptions[selectedElementKey]?.assertionType ?? "region_dimension"}
                onChange={(event) => {
                  const value = event.target.value as "region_dimension" | "measurement";
                  setElementOptions((state) => ({
                    ...state,
                    [selectedElementKey]: {
                      ...state[selectedElementKey],
                      assertionType: value,
                    },
                  }));
                }}
              >
                <option value="region_dimension">region_dimension</option>
                <option value="measurement">measurement</option>
              </select>
            </label>
          </div>
        ) : (
          <p className="muted mt-3 text-sm">Select a detected box to inspect its properties.</p>
        )}

        <div className="mt-6">
          <h3 className="text-xs font-bold uppercase tracking-[0.14em] text-slate-400">Custom Regions</h3>
          <div className="mt-2 max-h-[380px] space-y-2 overflow-auto">
            {customRegions.length === 0 && <p className="muted text-sm">No custom regions yet.</p>}
            {customRegions.map((region) => (
              <div key={region.id} className="rounded-sm border border-white/10 bg-black/30 p-2 text-xs">
                <input
                  value={region.name}
                  onChange={(event) => {
                    const value = event.target.value;
                    setCustomRegions((regions) =>
                      regions.map((item) => (item.id === region.id ? { ...item, name: value } : item)),
                    );
                  }}
                  className="mb-2 w-full rounded-sm border border-white/20 bg-black/40 px-2 py-1"
                />
                <div className="muted text-[11px]">
                  {region.rect.x.toFixed(0)}, {region.rect.y.toFixed(0)}, {region.rect.width.toFixed(0)} x {region.rect.height.toFixed(0)}
                </div>
                <div className="mt-2 flex items-center justify-between gap-2">
                  <label className="flex items-center gap-2">
                    tol
                    <input
                      type="number"
                      min={0}
                      value={region.tolerance}
                      className="w-16 rounded-sm border border-white/20 bg-black/40 px-1 py-1"
                      onChange={(event) => {
                        const value = Number(event.target.value);
                        setCustomRegions((regions) =>
                          regions.map((item) => (item.id === region.id ? { ...item, tolerance: value } : item)),
                        );
                      }}
                    />
                  </label>

                  <button
                    type="button"
                    className="rounded-sm border border-warn/60 px-2 py-1 text-warn hover:bg-warn/10"
                    onClick={() => setCustomRegions((regions) => regions.filter((item) => item.id !== region.id))}
                  >
                    remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </aside>
    </section>
  );
}
