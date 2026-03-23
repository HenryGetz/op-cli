export type BBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type ParsedElement = {
  index: number;
  element_id?: string;
  label?: string;
  element_type?: string;
  confidence?: number;
  bbox: BBox;
};

export type CheckResult = {
  id: string;
  passed: boolean | null;
  expected?: number;
  actual?: number;
  delta?: number;
  details?: string;
  [key: string]: unknown;
};

export type CaliperPayload = {
  status: "success" | "error";
  error: Record<string, unknown> | null;
  http_status?: number;
  image_width?: number;
  image_height?: number;
  meta?: {
    image_width?: number;
    image_height?: number;
    [key: string]: unknown;
  };
  summary?: {
    total: number;
    passed: number;
    failed: number;
    skipped: number;
  };
  results?: CheckResult[];
  elements?: ParsedElement[];
  baseline?: {
    config?: Record<string, unknown>;
    [key: string]: unknown;
  };
  diff?: {
    similarity_score?: number;
    added_elements?: unknown[];
    removed_elements?: unknown[];
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

export type HistoryItem = {
  id: string;
  projectName: string;
  timestamp: string;
  thumbnailDataUrl: string;
  passCount: number;
  failCount: number;
  changedAssertionIds: string[];
  checkPayload: CaliperPayload;
  referenceDataUrl?: string;
  iterationDataUrl?: string;
  configText?: string;
};
