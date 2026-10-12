/**
 * Pure projection of a `quadringent.diagnostic.v1` report for the cockpit.
 *
 * Rules kept here, not in the component:
 *  - green only for a `passed` result that is not synthetic and whose evidence
 *    kind is absent or live; a failure is always an incident; unknown and
 *    skipped stay neutral unless the data is synthetic or historical;
 *  - problems are listed first (failed, then required checks without an
 *    answer) and never merged into a global "healthy" verdict.
 */
import {
  DIAGNOSTIC_LAYERS,
  type DiagnosticLayer,
  type DiagnosticOverallStatus,
  type DiagnosticReport,
  type DiagnosticResult,
  type DiagnosticStatus,
  type DiagnosticSubject,
} from '../data/diagnosticsContract.ts';
import type { Route } from '../router.ts';
import { DIAGNOSTICS_COPY } from './operator.ts';
import { timestamp } from './format.ts';

export type DiagnosticTone = 'ok' | 'incident' | 'neutral' | 'limit';

export interface DiagnosticResultView {
  readonly result: DiagnosticResult;
  readonly tone: DiagnosticTone;
  readonly statusWord: string;
  readonly qualifier: string | null;
  readonly established: 'problem' | 'gap' | null;
  readonly subjectText: string | null;
  readonly subjectRoute: Route | null;
  readonly sinceText: string | null;
  readonly observedText: string | null;
}

export interface DiagnosticLayerGroup {
  readonly layer: DiagnosticLayer;
  readonly label: string;
  readonly views: readonly DiagnosticResultView[];
}

export interface DiagnosticCounts {
  readonly passed: number;
  readonly failed: number;
  readonly unknown: number;
  readonly skipped: number;
  readonly total: number;
}

export interface DiagnosticsView {
  readonly overall: { readonly status: DiagnosticOverallStatus; readonly text: string };
  readonly counts: DiagnosticCounts;
  readonly countsConsistent: boolean;
  readonly problems: readonly DiagnosticResultView[];
  readonly gaps: readonly DiagnosticResultView[];
  readonly groups: readonly DiagnosticLayerGroup[];
  readonly empty: boolean;
}

export interface EvidenceRow {
  readonly key: string;
  readonly label: string;
  readonly value: string;
}

const STATUS_RANK: Readonly<Record<DiagnosticStatus, number>> = { failed: 0, unknown: 1, skipped: 2, passed: 3 };
const LAYER_RANK: Readonly<Record<DiagnosticLayer, number>> = Object.fromEntries(
  DIAGNOSTIC_LAYERS.map((layer, index) => [layer, index]),
) as Record<DiagnosticLayer, number>;
const OVERALL_RANK: Readonly<Record<DiagnosticOverallStatus, number>> = { ready: 0, incomplete: 1, blocked: 2 };

/** Why a result is not a live measurement; `null` when it is one. */
export function limitQualifier(result: DiagnosticResult, reportSynthetic: boolean): string | null {
  if (reportSynthetic || result.evidenceKind === 'simulation') return DIAGNOSTICS_COPY.qualifier.synthetic;
  if (result.evidenceKind === 'historical') return DIAGNOSTICS_COPY.qualifier.historical;
  if (result.evidenceKind === 'absent') return DIAGNOSTICS_COPY.qualifier.absent;
  return null;
}

export function diagnosticTone(result: DiagnosticResult, report: Pick<DiagnosticReport, 'synthetic'>): DiagnosticTone {
  if (result.status === 'failed') return 'incident';
  const limited = limitQualifier(result, report.synthetic) !== null;
  if (result.status === 'passed') return limited ? 'limit' : 'ok';
  return limited ? 'limit' : 'neutral';
}

export function subjectRoute(subject: DiagnosticSubject | null): Route | null {
  if (!subject) return null;
  if (subject.kind === 'pipeline') return { name: 'cockpit-table', id: subject.ref };
  if (subject.kind === 'source') return { name: 'cockpit-connection', id: subject.ref };
  return null;
}

export function evidenceRows(result: DiagnosticResult): readonly EvidenceRow[] {
  return Object.entries(result.evidence)
    .filter(([key]) => key !== 'evidence_kind')
    .map(([key, value]) => ({ key, label: humanize(key), value: scalarText(value) }));
}

function humanize(key: string): string {
  const spaced = key.replace(/[_.-]+/g, ' ').trim();
  return spaced ? spaced[0]!.toUpperCase() + spaced.slice(1) : key;
}

function scalarText(value: string | number | boolean | null): string {
  if (value === null) return '—';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return String(value);
}

function utc(value: string | null): string | null {
  return value === null ? null : timestamp(value);
}

function toView(result: DiagnosticResult, report: DiagnosticReport): DiagnosticResultView {
  const required = result.required;
  const established = result.status === 'failed'
    ? 'problem'
    : required && (result.status === 'unknown' || result.status === 'skipped') ? 'gap' : null;
  const kindLabel = result.subject ? DIAGNOSTICS_COPY.subjectKinds[result.subject.kind] : null;
  return {
    result,
    tone: diagnosticTone(result, report),
    statusWord: DIAGNOSTICS_COPY.status[result.status],
    qualifier: limitQualifier(result, report.synthetic),
    established,
    subjectText: result.subject ? `${kindLabel}: ${result.subject.label ?? result.subject.ref}` : null,
    subjectRoute: subjectRoute(result.subject),
    sinceText: utc(result.since),
    observedText: utc(result.observedAt),
  };
}

function compareViews(a: DiagnosticResultView, b: DiagnosticResultView): number {
  return LAYER_RANK[a.result.layer] - LAYER_RANK[b.result.layer]
    || STATUS_RANK[a.result.status] - STATUS_RANK[b.result.status]
    || a.result.code.localeCompare(b.result.code);
}

function countResults(results: readonly DiagnosticResult[]): DiagnosticCounts {
  const counts = { passed: 0, failed: 0, unknown: 0, skipped: 0 };
  for (const result of results) counts[result.status] += 1;
  return { ...counts, total: results.length };
}

/** Derived from the listed results, with the contract's rule; the service's
 *  own status is kept only when it is more severe. */
function overallStatus(report: DiagnosticReport, counts: DiagnosticCounts): DiagnosticOverallStatus {
  const requiredGap = report.results.some((item) => item.required && (item.status === 'unknown' || item.status === 'skipped'));
  const derived: DiagnosticOverallStatus = counts.failed > 0 ? 'blocked' : requiredGap ? 'incomplete' : 'ready';
  return OVERALL_RANK[report.overall.status] > OVERALL_RANK[derived] ? report.overall.status : derived;
}

export function buildDiagnosticsView(report: DiagnosticReport): DiagnosticsView {
  const views = report.results.map((result) => toView(result, report)).sort(compareViews);
  const counts = countResults(report.results);
  const status = overallStatus(report, counts);
  const text = status === 'ready' && report.synthetic
    ? DIAGNOSTICS_COPY.overall.readySynthetic
    : DIAGNOSTICS_COPY.overall[status];
  const groups: DiagnosticLayerGroup[] = [];
  for (const layer of DIAGNOSTIC_LAYERS) {
    const inLayer = views.filter((item) => item.result.layer === layer);
    if (inLayer.length > 0) groups.push({ layer, label: DIAGNOSTICS_COPY.layers[layer], views: inLayer });
  }
  return {
    overall: { status, text },
    counts,
    countsConsistent: report.overall.passed === counts.passed && report.overall.failed === counts.failed
      && report.overall.unknown === counts.unknown && report.overall.skipped === counts.skipped,
    problems: views.filter((item) => item.established === 'problem'),
    gaps: views.filter((item) => item.established === 'gap'),
    groups,
    empty: views.length === 0,
  };
}

export type DiagnosticsFailureKind = keyof typeof DIAGNOSTICS_COPY.errors;

export interface DiagnosticsFailure {
  readonly kind: DiagnosticsFailureKind;
  readonly title: string;
  readonly detail: string;
  /** The service's own words, shown only when it sent a usable message. */
  readonly serviceMessage: string | null;
  readonly retryAfterSeconds: number | null;
}

/** Explains a failed diagnostics request. The kind comes from the HTTP status
 *  (`ControlPlaneV2Error.kind`), never from parsing message text. */
export function describeDiagnosticsError(error: unknown): DiagnosticsFailure {
  const candidate = (typeof error === 'object' && error !== null ? error : {}) as {
    readonly kind?: unknown;
    readonly message?: unknown;
    readonly retryAfterSeconds?: unknown;
  };
  const kind: DiagnosticsFailureKind = typeof candidate.kind === 'string' && candidate.kind in DIAGNOSTICS_COPY.errors
    ? candidate.kind as DiagnosticsFailureKind
    : 'unavailable';
  const copy = DIAGNOSTICS_COPY.errors[kind];
  const retryAfter = typeof candidate.retryAfterSeconds === 'number' && candidate.retryAfterSeconds >= 0
    ? candidate.retryAfterSeconds
    : null;
  const showMessage = kind === 'invalid_response' || kind === 'rejected' || kind === 'conflict';
  return {
    kind,
    title: copy.title,
    detail: copy.detail,
    serviceMessage: showMessage && typeof candidate.message === 'string' && candidate.message.trim() ? candidate.message : null,
    retryAfterSeconds: kind === 'rate_limited' || kind === 'unavailable' ? retryAfter : null,
  };
}
