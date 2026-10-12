/**
 * Diagnostic export: integrity check against the preview, and an in-memory
 * download. Nothing is written to persistent browser storage; the object URL
 * lives only as long as the browser needs it and is revoked afterwards.
 */
import type { DiagnosticExport } from '../data/diagnosticsContract.ts';
import { DIAGNOSTICS_COPY } from './operator.ts';

export type Sha256Hex = (text: string) => Promise<string>;

export interface ExportIntegrity {
  /** Mismatches between the preview and the content. Empty means none found. */
  readonly problems: readonly string[];
  /** False when the digests could not be computed in this browser. */
  readonly verified: boolean;
}

export const browserSha256: Sha256Hex | null =
  typeof globalThis.crypto?.subtle?.digest === 'function'
    ? async (text) => {
        const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
        return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
      }
    : null;

export async function checkExportIntegrity(
  exported: DiagnosticExport,
  sha256: Sha256Hex | null = browserSha256,
): Promise<ExportIntegrity> {
  const copy = DIAGNOSTICS_COPY.export.integrity;
  const problems: string[] = [];
  const listed = new Set(exported.preview.files.map((file) => file.name));
  for (const file of exported.preview.files) {
    const text = exported.files[file.name];
    if (text === undefined) {
      problems.push(copy.missing(file.name));
      continue;
    }
    if (new TextEncoder().encode(text).length !== file.bytes) problems.push(copy.size(file.name));
    if (sha256 && (await sha256(text)) !== file.sha256) problems.push(copy.digest(file.name));
  }
  for (const name of Object.keys(exported.files)) {
    if (!listed.has(name)) problems.push(copy.unlisted(name));
  }
  return { problems, verified: sha256 !== null };
}

/** How many results `report.json` holds, or `null` when it cannot be read. */
export function exportResultCount(exported: DiagnosticExport): number | null {
  const text = exported.files['report.json'];
  if (text === undefined) return null;
  try {
    const results = (JSON.parse(text) as { results?: unknown }).results;
    return Array.isArray(results) ? results.length : null;
  } catch {
    return null;
  }
}

/** Only the digits of the UTC generation time reach the file name. */
export function exportFileName(generatedAt: string): string {
  const compact = new Date(generatedAt).toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
  return `quadringent-diagnostic-export-${compact}.json`;
}

/** The received export, re-serialized as received: schema, time, preview, files. */
export function exportBundleText(exported: DiagnosticExport): string {
  return JSON.stringify({
    schema: exported.schema,
    generated_at: exported.generatedAt,
    preview: {
      files: exported.preview.files.map((file) => ({ name: file.name, bytes: file.bytes, sha256: file.sha256 })),
      omitted: exported.preview.omitted.map((item) => ({ field: item.field, reason: item.reason })),
      pseudonymized: exported.preview.pseudonymized,
      limits: exported.preview.limits,
    },
    files: exported.files,
  }, null, 2);
}

export interface BlobDownloadEnv {
  readonly createObjectURL: (blob: Blob) => string;
  readonly revokeObjectURL: (url: string) => void;
  /** Hands the object URL to the browser as a file download. */
  readonly click: (url: string, fileName: string) => void;
  /** Runs `callback` shortly after the click. */
  readonly schedule: (callback: () => void) => void;
}

export function downloadExport(exported: DiagnosticExport, env: BlobDownloadEnv): void {
  const blob = new Blob([exportBundleText(exported)], { type: 'application/json' });
  const url = env.createObjectURL(blob);
  try {
    env.click(url, exportFileName(exported.generatedAt));
  } finally {
    env.schedule(() => env.revokeObjectURL(url));
  }
}

/** Browser environment for `downloadExport`; created at call time. */
export function browserDownloadEnv(): BlobDownloadEnv {
  return {
    createObjectURL: (blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
    click: (url, fileName) => {
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = fileName;
      anchor.rel = 'noopener';
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      try {
        anchor.click();
      } finally {
        anchor.remove();
      }
    },
    schedule: (callback) => { setTimeout(callback, 1000); },
  };
}
