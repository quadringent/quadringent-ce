/**
 * Strict parsers for the diagnostic contracts served by `/v2/diagnostics/report`
 * (`quadringent.diagnostic.v1`) and `/v2/diagnostics/report/export`
 * (`quadringent.diagnostic-export.v1`).
 *
 * A diagnostic is evidence about one layer of the data path. Anything this
 * console cannot interpret with certainty (unknown schema, status, layer or
 * evidence kind) is rejected as a whole: a partially understood report must
 * never be shown as if it were complete.
 */

export const DIAGNOSTIC_SCHEMA = 'quadringent.diagnostic.v1';
export const DIAGNOSTIC_EXPORT_SCHEMA = 'quadringent.diagnostic-export.v1';

export const DIAGNOSTIC_LAYERS = [
  'local_tools',
  'local_config',
  'network',
  'tls',
  'authentication',
  'source_prerequisites',
  'destination',
  'control_plane',
  'capture',
  'delivery',
  'reconciliation',
  'evidence_freshness',
] as const;
export type DiagnosticLayer = (typeof DIAGNOSTIC_LAYERS)[number];

export const DIAGNOSTIC_STATUSES = ['passed', 'failed', 'unknown', 'skipped'] as const;
export type DiagnosticStatus = (typeof DIAGNOSTIC_STATUSES)[number];

export const DIAGNOSTIC_OVERALL_STATUSES = ['ready', 'blocked', 'incomplete'] as const;
export type DiagnosticOverallStatus = (typeof DIAGNOSTIC_OVERALL_STATUSES)[number];

export const DIAGNOSTIC_SUBJECT_KINDS = [
  'source', 'destination', 'pipeline', 'table', 'host', 'tool', 'control_plane',
] as const;
export type DiagnosticSubjectKind = (typeof DIAGNOSTIC_SUBJECT_KINDS)[number];

export const DIAGNOSTIC_EVIDENCE_KINDS = ['live', 'historical', 'simulation', 'absent'] as const;
export type DiagnosticEvidenceKind = (typeof DIAGNOSTIC_EVIDENCE_KINDS)[number];

export type DiagnosticScope = 'local' | 'control_plane';
export type DiagnosticScalar = string | number | boolean | null;

export interface DiagnosticSubject {
  readonly kind: DiagnosticSubjectKind;
  readonly ref: string;
  readonly label: string | null;
}

export interface DiagnosticResult {
  readonly code: string;
  readonly layer: DiagnosticLayer;
  readonly status: DiagnosticStatus;
  readonly required: boolean;
  readonly observedAt: string | null;
  readonly provenance: string;
  readonly subject: DiagnosticSubject | null;
  readonly since: string | null;
  readonly summary: string;
  readonly nextAction: string | null;
  readonly limits: string | null;
  /** Flat, allow-listed scalars. */
  readonly evidence: Readonly<Record<string, DiagnosticScalar>>;
  /** `evidence.evidence_kind` when the service reported one; `null` when absent. */
  readonly evidenceKind: DiagnosticEvidenceKind | null;
}

export interface DiagnosticOverall {
  readonly status: DiagnosticOverallStatus;
  readonly passed: number;
  readonly failed: number;
  readonly unknown: number;
  readonly skipped: number;
  readonly summary: string;
}

export interface DiagnosticReport {
  readonly schema: typeof DIAGNOSTIC_SCHEMA;
  readonly generatedAt: string;
  readonly scope: DiagnosticScope;
  readonly synthetic: boolean;
  readonly software: { readonly product: string; readonly version: string; readonly distribution: string };
  readonly overall: DiagnosticOverall;
  readonly results: readonly DiagnosticResult[];
}

export interface DiagnosticExportFile {
  readonly name: string;
  readonly bytes: number;
  readonly sha256: string;
}

export interface DiagnosticExportOmission {
  readonly field: string;
  readonly reason: string;
}

export interface DiagnosticExport {
  readonly schema: typeof DIAGNOSTIC_EXPORT_SCHEMA;
  readonly generatedAt: string;
  readonly preview: {
    readonly files: readonly DiagnosticExportFile[];
    readonly omitted: readonly DiagnosticExportOmission[];
    readonly pseudonymized: Readonly<Record<string, number>>;
    readonly limits: readonly string[];
  };
  readonly files: Readonly<Record<string, string>>;
}

export class DiagnosticContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'DiagnosticContractError';
  }
}

function invalid(what: string): never {
  throw new DiagnosticContractError(`Invalid diagnostic payload: ${what}.`);
}

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return invalid(what);
  return value as Record<string, unknown>;
}

function asString(value: unknown, what: string): string {
  if (typeof value !== 'string') return invalid(what);
  return value;
}

function asNonEmptyString(value: unknown, what: string): string {
  if (typeof value !== 'string' || !value.trim()) return invalid(what);
  return value;
}

function asNullableString(value: unknown, what: string): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'string') return invalid(what);
  return value.trim() ? value : null;
}

function asTimestamp(value: unknown, what: string): string {
  if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value))) {
    return invalid(what);
  }
  return value;
}

function asNullableTimestamp(value: unknown, what: string): string | null {
  if (value === undefined || value === null) return null;
  return asTimestamp(value, what);
}

function asCount(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) return invalid(what);
  return value;
}

function asBoolean(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') return invalid(what);
  return value;
}

function oneOf<T extends string>(allowed: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) return invalid(what);
  return value as T;
}

function parseSubject(value: unknown): DiagnosticSubject | null {
  if (value === undefined || value === null) return null;
  const record = asRecord(value, 'subject');
  return {
    kind: oneOf(DIAGNOSTIC_SUBJECT_KINDS, record.kind, 'subject kind'),
    ref: asNonEmptyString(record.ref, 'subject ref'),
    label: asNullableString(record.label, 'subject label'),
  };
}

function parseEvidence(value: unknown): {
  readonly evidence: Readonly<Record<string, DiagnosticScalar>>;
  readonly evidenceKind: DiagnosticEvidenceKind | null;
} {
  if (value === undefined || value === null) return { evidence: {}, evidenceKind: null };
  const record = asRecord(value, 'evidence');
  const evidence: Record<string, DiagnosticScalar> = {};
  for (const [key, scalar] of Object.entries(record)) {
    if (scalar !== null && typeof scalar !== 'string' && typeof scalar !== 'number' && typeof scalar !== 'boolean') {
      return invalid(`evidence value for ${key} must be a scalar`);
    }
    evidence[key] = scalar;
  }
  const kind = 'evidence_kind' in record
    ? oneOf(DIAGNOSTIC_EVIDENCE_KINDS, record.evidence_kind, 'evidence kind')
    : null;
  return { evidence, evidenceKind: kind };
}

function parseResult(value: unknown): DiagnosticResult {
  const record = asRecord(value, 'result');
  const { evidence, evidenceKind } = parseEvidence(record.evidence);
  return {
    code: asNonEmptyString(record.code, 'result code'),
    layer: oneOf(DIAGNOSTIC_LAYERS, record.layer, 'result layer'),
    status: oneOf(DIAGNOSTIC_STATUSES, record.status, 'result status'),
    required: asBoolean(record.required, 'result required flag'),
    observedAt: asNullableTimestamp(record.observed_at, 'result observed_at'),
    provenance: asNonEmptyString(record.provenance, 'result provenance'),
    subject: parseSubject(record.subject),
    since: asNullableTimestamp(record.since, 'result since'),
    summary: asNonEmptyString(record.summary, 'result summary'),
    nextAction: asNullableString(record.next_action, 'result next_action'),
    limits: asNullableString(record.limits, 'result limits'),
    evidence,
    evidenceKind,
  };
}

export function parseDiagnosticReport(value: unknown): DiagnosticReport {
  const record = asRecord(value, 'report');
  if (record.schema !== DIAGNOSTIC_SCHEMA) return invalid(`unsupported schema ${JSON.stringify(record.schema)}`);
  const software = asRecord(record.software, 'software');
  const overall = asRecord(record.overall, 'overall');
  if (!Array.isArray(record.results)) return invalid('results must be a list');
  return {
    schema: DIAGNOSTIC_SCHEMA,
    generatedAt: asTimestamp(record.generated_at, 'generated_at'),
    scope: oneOf(['local', 'control_plane'] as const, record.scope, 'scope'),
    synthetic: asBoolean(record.synthetic, 'synthetic flag'),
    software: {
      // The service only promises an object of strings; absent parts read as empty.
      product: software.product === undefined ? '' : asString(software.product, 'software product'),
      version: software.version === undefined ? '' : asString(software.version, 'software version'),
      distribution: software.distribution === undefined ? '' : asString(software.distribution, 'software distribution'),
    },
    overall: {
      status: oneOf(DIAGNOSTIC_OVERALL_STATUSES, overall.status, 'overall status'),
      passed: asCount(overall.passed, 'overall passed count'),
      failed: asCount(overall.failed, 'overall failed count'),
      unknown: asCount(overall.unknown, 'overall unknown count'),
      skipped: asCount(overall.skipped, 'overall skipped count'),
      summary: asString(overall.summary, 'overall summary'),
    },
    results: record.results.map(parseResult),
  };
}

const SHA256 = /^[0-9a-f]{64}$/;
const SAFE_FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function parseDiagnosticExport(value: unknown): DiagnosticExport {
  const record = asRecord(value, 'export');
  if (record.schema !== DIAGNOSTIC_EXPORT_SCHEMA) return invalid(`unsupported schema ${JSON.stringify(record.schema)}`);
  const preview = asRecord(record.preview, 'preview');
  const files = asRecord(record.files, 'files');
  if (!Array.isArray(preview.files) || !Array.isArray(preview.omitted) || !Array.isArray(preview.limits)) {
    return invalid('preview lists');
  }
  const previewFiles = preview.files.map((item): DiagnosticExportFile => {
    const file = asRecord(item, 'preview file');
    const name = asString(file.name, 'preview file name');
    if (!SAFE_FILE_NAME.test(name)) return invalid('preview file name');
    const sha256 = asString(file.sha256, 'preview file sha256');
    if (!SHA256.test(sha256)) return invalid('preview file sha256');
    return { name, bytes: asCount(file.bytes, 'preview file size'), sha256 };
  });
  const names = new Set<string>();
  for (const file of previewFiles) {
    if (names.has(file.name)) return invalid('duplicate preview file');
    names.add(file.name);
  }
  const contents: Record<string, string> = {};
  for (const [name, text] of Object.entries(files)) {
    contents[name] = asString(text, `file content for ${name}`);
  }
  const pseudonymizedRecord = asRecord(preview.pseudonymized, 'pseudonymized');
  const pseudonymized: Record<string, number> = {};
  for (const [key, count] of Object.entries(pseudonymizedRecord)) {
    pseudonymized[key] = asCount(count, `pseudonymized count for ${key}`);
  }
  return {
    schema: DIAGNOSTIC_EXPORT_SCHEMA,
    generatedAt: asTimestamp(record.generated_at, 'generated_at'),
    preview: {
      files: previewFiles,
      omitted: preview.omitted.map((item): DiagnosticExportOmission => {
        const omission = asRecord(item, 'omission');
        return {
          field: asNonEmptyString(omission.field, 'omission field'),
          reason: asNonEmptyString(omission.reason, 'omission reason'),
        };
      }),
      pseudonymized,
      limits: preview.limits.map((limit) => asNonEmptyString(limit, 'limit')),
    },
    files: contents,
  };
}
