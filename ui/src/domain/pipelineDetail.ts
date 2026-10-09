import type { Coverage, EvidenceKind, Freshness, Incident, LagPoint, Pipeline, Stage, StageId, StageStatus } from './controlPlane.ts';

export interface StageProofView {
  readonly statusLabel: string;
  readonly observedAt: string | null;
  readonly provenance: string;
}

export interface IncidentContextView {
  readonly publicLabel: string;
  readonly safeExplanation: string;
  readonly affectedStage: "Reader" | 'Destination';
  readonly firstObservedAt: null;
  readonly lastObservedAt: string;
  readonly recommendationState: 'no_safe_recommendation';
}

interface FocusTarget {
  focus(): void;
}

type FocusScheduler = (callback: () => void) => void;

export function resolveSelectedStage(pipeline: Pipeline, selectedId: StageId | null): Stage | null {
  if (selectedId === null) return null;
  return pipeline.stages.find((stage) => stage.id === selectedId) ?? null;
}

export function restoreSelectedStageFocus(
  stageButtons: ReadonlyMap<StageId, FocusTarget>,
  selectedId: StageId | null,
  schedule: FocusScheduler = queueMicrotask,
): void {
  if (selectedId === null) return;
  schedule(() => stageButtons.get(selectedId)?.focus());
}

export function incidentPublicLabel(incident: Incident): string {
  switch (incident.type) {
    case 'capture_connection_failure': return "Source connection interrupted";
    case 'capture_timeout': return "Read timeout exceeded";
    case 'capture_stopped': return "Reader stopped safely";
    case 'capture_auth_blocked': return "Source authentication blocked";
    case 'capture_position_review': return "Recovery position needs review";
    case 'destination': return "Destination incident";
  }
}

export function buildLagSegments(points: readonly LagPoint[]): readonly (readonly LagPoint[])[] {
  const segments: LagPoint[][] = [];
  let current: LagPoint[] = [];
  for (const point of points) {
    if (point.coverage === 'gap' || point.lag === null) {
      if (current.length > 0) segments.push(current);
      current = [];
      continue;
    }
    current.push(point);
  }
  if (current.length > 0) segments.push(current);
  return segments;
}

export function lagCoverageLabel(point: LagPoint): string {
  if (point.kind === 'temporal_gap') return "Interval not observed";
  if (point.coverage === 'gap') return `Missing · ${point.unknownSamples} unknown`;
  return "Measured";
}

export interface SalientLagPreview {
  readonly points: readonly LagPoint[];
  readonly caption: string;
  readonly complete: boolean;
}

export function sameLagWindow(left: LagPoint, right: LagPoint): boolean {
  return left.startSeconds === right.startSeconds && left.endSeconds === right.endSeconds;
}

export function selectSalientLagWindows(points: readonly LagPoint[]): SalientLagPreview {
  if (points.length <= 3) {
    return {
      points,
      caption: "All intervals, synchronized with the chart",
      complete: true,
    };
  }

  const first = points[0]!;
  const last = points[points.length - 1]!;
  let maximum = first;
  for (const point of points) {
    if ((point.lag ?? Number.NEGATIVE_INFINITY) > (maximum.lag ?? Number.NEGATIVE_INFINITY)) {
      maximum = point;
    }
  }
  const lastZero = [...points].reverse().find((point) => point.lag === 0) ?? last;
  const lastNonZero = [...points].reverse().find((point) => point.lag !== null && point.lag > 0);

  const selected: LagPoint[] = [];
  const add = (point: LagPoint | undefined) => {
    if (!point) return;
    if (selected.some((item) => sameLagWindow(item, point))) return;
    selected.push(point);
  };
  add(first);
  add(maximum);
  add(lastZero);
  if (selected.length < 3) add(lastNonZero);
  if (selected.length < 3) add(last);
  if (selected.length < 3) add(points[Math.floor((points.length - 1) / 2)]);
  selected.sort((left, right) => left.startSeconds - right.startSeconds || left.endSeconds - right.endSeconds);

  const maxIsFirst = sameLagWindow(maximum, first);
  const stopIsZero = lastZero.lag === 0;
  const caption = stopIsZero
    ? maxIsFirst
      ? "Key points: start (maximum), decline and stop at zero, synchronized with the chart"
      : "Key points: start, maximum and last zero interval, synchronized with the chart"
    : maxIsFirst
      ? "Key points: start (maximum) and last interval, synchronized with the chart"
      : "Key points: start, maximum and last interval, synchronized with the chart";

  return { points: selected.slice(0, 3), caption, complete: false };
}

export function buildStageProof(stage: Stage, pipeline: Pipeline): StageProofView {
  let statusLabel: string;
  if (stage.status === 'healthy' && pipeline.quality.evidenceKind === 'simulation') {
    statusLabel = "Observed in the demonstration";
  } else if (stage.status === 'healthy' && pipeline.quality.evidenceKind === 'historical') {
    statusLabel = "Observed in the latest observation";
  } else if (stage.status === 'healthy') {
    statusLabel = "Stage observed";
  } else if (stage.status === 'incident') {
    statusLabel = "Incident observed";
  } else if (stage.status === 'degraded') {
    statusLabel = "Partial observation";
  } else if (stage.status === 'planned_stop') {
    statusLabel = "Stop observed";
  } else if (stage.status === 'awaiting_resume') {
    statusLabel = "Ready to resume";
  } else if (stage.observedAt !== null) {
    statusLabel = "Unconfirmed";
  } else {
    statusLabel = "Observation absent";
  }
  return {
    statusLabel,
    observedAt: stage.observedAt,
    provenance: `service / ${pipeline.id} / ${dataNatureLabel(pipeline.quality.evidenceKind)}`,
  };
}

export function stageLabel(stage: Stage['id']): string {
  switch (stage) {
    case 'source': return 'Source';
    case 'capture': return "Reader";
    case 'raw': return "Reception area";
    case 'load': return "Initial copy";
    case 'destination': return 'Destination';
  }
}

export function stageStatusLabel(status: StageStatus): string {
  switch (status) {
    case 'healthy': return "Available";
    case 'degraded': return "Partial";
    case 'incident': return "To verify";
    case 'planned_stop': return "Paused";
    case 'awaiting_resume': return "Ready to resume";
    case 'unknown': return "Unavailable";
  }
}

export function coverageLabel(coverage: Coverage): string {
  switch (coverage) {
    case 'complete': return "Complete";
    case 'partial': return "Partial";
    case 'gap': return "Incomplete";
    case 'none': return "Absent";
  }
}

export function freshnessLabel(freshness: Freshness): string {
  switch (freshness) {
    case 'fresh': return "Recent";
    case 'late': return "Lagging";
    case 'stale': return "Too old";
    case 'clock_untrusted': return "Clock unreliable";
  }
}

export function dataNatureLabel(kind: EvidenceKind): string {
  switch (kind) {
    case 'live': return "Current reading";
    case 'historical': return "Latest observation";
    case 'simulation': return "Demonstration";
  }
}

export function buildIncidentContext(pipeline: Pipeline): IncidentContextView | null {
  if (!pipeline.incident) return null;
  const destinationIncident = pipeline.incident.type === 'destination';
  const safeExplanation = pipeline.incident.type === 'capture_connection_failure'
    ? "The reader can no longer confirm the source connection."
    : pipeline.incident.type === 'capture_timeout'
      ? "The reader exceeded the allowed timeout."
      : pipeline.incident.type === 'capture_auth_blocked'
        ? "The source rejected the connection: fix the account, then reset the guard."
        : pipeline.incident.type === 'capture_position_review'
          ? "The recovery point no longer allows a safe window: re-anchor the checkpoint."
          : destinationIncident
            ? "Destination arrival evidence is contradictory or failed."
            : "The reader stopped under the safety stop rule.";
  return {
    publicLabel: incidentPublicLabel(pipeline.incident),
    safeExplanation,
    affectedStage: destinationIncident ? 'Destination' : "Reader",
    firstObservedAt: null,
    lastObservedAt: pipeline.observedAt,
    recommendationState: 'no_safe_recommendation',
  };
}
