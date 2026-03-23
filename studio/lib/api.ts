import type { CaliperPayload } from "@/lib/types";

const SERVER = process.env.NEXT_PUBLIC_CALIPER_SERVER ?? "http://localhost:7771";

export async function postCaliperForm(path: string, formData: FormData): Promise<CaliperPayload> {
  const response = await fetch(`${SERVER}${path}`, {
    method: "POST",
    body: formData,
  });

  const payload = (await response.json()) as CaliperPayload;
  payload.http_status = response.status;
  return payload;
}

export async function getCaliper(path: string): Promise<CaliperPayload> {
  const response = await fetch(`${SERVER}${path}`);
  const payload = (await response.json()) as CaliperPayload;
  payload.http_status = response.status;
  return payload;
}

export function payloadError(payload: CaliperPayload): string {
  if (payload.status !== "error") {
    return "";
  }

  if (payload.error && typeof payload.error.message === "string") {
    return payload.error.message;
  }
  return "Unknown server error";
}

export function toDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("Failed to read file"));
    reader.readAsDataURL(file);
  });
}

export function dataUrlToFile(dataUrl: string, filename: string): File {
  const [meta, encoded] = dataUrl.split(",");
  const mimeMatch = /data:([^;]+);base64/.exec(meta ?? "");
  const mime = mimeMatch?.[1] ?? "image/png";
  const binary = atob(encoded ?? "");
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new File([bytes], filename, { type: mime });
}
