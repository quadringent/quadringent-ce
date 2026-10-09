import type { ControlPlaneState } from './data/controlPlaneController.ts';
import type { Pipeline } from './domain/controlPlane.ts';
import { decimal, formatDuration } from './domain/format.ts';
import { sourceAvailability } from './domain/scope.ts';

const integerFormat = new Intl.NumberFormat("en-US");

export interface SummaryMetric {
  readonly value: string;
  readonly unknownReason?: string;
}

export interface CounterMetric {
  readonly label: string;
  readonly value: string;
  readonly unknownReason?: string;
}

export interface WorkspaceSummary {
  readonly pipelines: SummaryMetric;
  readonly attention: SummaryMetric;
  readonly incidents: SummaryMetric;
  readonly recoveries: SummaryMetric;
  readonly latestObservation: SummaryMetric;
  readonly empty: {
    readonly title: string;
    readonly description: string;
  };
}

export function buildWorkspaceSummary(
  state: ControlPlaneState,
  pipelines: readonly Pipeline[],
): WorkspaceSummary {
  if (state.status === 'loading' || state.status === 'failed') {
    return {
      pipelines: unknownMetric(),
      attention: unknownMetric(),
      incidents: unknownMetric(),
      recoveries: unknownMetric(),
      latestObservation: unknownMetric(),
      empty: {
        title: "Data unavailable",
        description: "No data can be confirmed right now.",
      },
    };
  }

  const attention = pipelines.filter((pipeline) => pipeline.status !== 'healthy').length;
  const incidents = pipelines.filter((pipeline) => pipeline.status === 'incident' || pipeline.incident !== null).length;
  const recoveries = pipelines.filter((pipeline) => pipeline.status === 'recovering').length;
  const latestObservation = latestObservedAt(pipelines);

  return {
    pipelines: knownMetric(pipelines.length),
    attention: knownMetric(attention),
    incidents: knownMetric(incidents),
    recoveries: knownMetric(recoveries),
    latestObservation: latestObservation
      ? knownMetric(formatTimestamp(latestObservation))
      : { value: '—' },
    empty: {
      title: pipelines.length ? "No pipeline requires attention" : "No pipeline observed",
      description: pipelines.length
        ? "The current observation reports no incident, partial coverage or ongoing recovery."
        : "No pipeline data is visible in this observation.",
    },
  };
}

export function buildPriorityMeta(state: ControlPlaneState, recoveringCount: number): string {
  if (state.status === 'loading') return "Verification in progress";
  if (state.status === 'failed') return "Data unavailable — no observation";
  return recoveringCount > 0 ? `${integerFormat.format(recoveringCount)} recovering` : "No recovery observed";
}

export function buildStatusNarrative(state: ControlPlaneState, now: Date = new Date()): string {
  switch (state.status) {
    case 'loading':
      return "Data unavailable · waiting for the first observation.";
    case 'failed':
      return `Data unavailable · ${sanitizeMessage(state.message)}`;
    case 'refreshing':
      return "Observation received · refresh in progress.";
    case 'ready':
      return "Observation received. The displayed information describes this observation, not confirmed delivery.";
    case 'degraded':
      return `Last observation retained ${ageLabel(state.lastSuccessAt, now)} · ${sanitizeMessage(state.message)}`;
  }
}

export function buildPipelineMissingCopy(
  state: ControlPlaneState,
  _pipelineId: string,
): { readonly title: string; readonly description: string } {
  if (state.status === 'loading' || state.status === 'failed') {
    return {
      title: "Data unavailable",
      description:
        state.status === 'loading'
          ? "Verification in progress — the console cannot confirm this pipeline yet."
          : "Data unavailable — the console cannot confirm this pipeline yet.",
    };
  }

  const confirmedCurrentInventory = state.status === 'ready'
    && state.connection === 'live'
    && 'overview' in state
    && state.overview.scope?.kind !== 'unavailable'
    && sourceAvailability(state.overview) === 'available';

  if (!confirmedCurrentInventory) {
    return {
      title: "Pipeline presence to confirm",
      description: "The available information cannot confirm whether this pipeline is part of current tracking.",
    };
  }

  return {
    title: "Pipeline not found",
    description: "The confirmed observation does not contain this pipeline.",
  };
}

export function buildPipelineLead(state: ControlPlaneState, pipeline: Pipeline | null): string {
  if (pipeline) return pipeline.summary;
  return buildPipelineMissingCopy(state, '').description;
}

export function buildCounterMetrics(counters: Readonly<Record<string, number | null>>): readonly CounterMetric[] {
  return Object.entries(counters).map(([key, value]) => (
    value === null
      ? {
          label: counterLabel(key),
          value: "Unknown",
          unknownReason: "Counter to confirm",
        }
      : {
          label: counterLabel(key),
          value: counterValue(key, value),
        }
  ));
}

function counterValue(key: string, value: number): string {
  switch (key) {
    case 'run_duration_s': return formatDuration(value);
    case 'cpu_ms_per_event': return `${decimal(value, 1)} ms`;
    case 'mean_mcpu': return `${decimal(value / 1000, 2)} core`;
    case 'payload_bytes_published': return `${integerFormat.format(value)} bytes`;
    default: return integerFormat.format(value);
  }
}

function knownMetric(value: number | string): SummaryMetric {
  return { value: typeof value === 'number' ? integerFormat.format(value) : value };
}

function unknownMetric(): SummaryMetric {
  return { value: "Unknown", unknownReason: "Data unavailable" };
}

function latestObservedAt(pipelines: readonly Pipeline[]): string | null {
  let latest: string | null = null;
  for (const pipeline of pipelines) {
    if (!latest || new Date(pipeline.observedAt).getTime() > new Date(latest).getTime()) latest = pipeline.observedAt;
  }
  return latest;
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-US", { hourCycle: 'h23',
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'UTC',
  }).format(new Date(value));
}

function ageLabel(observedAt: Date, now: Date): string {
  const minutes = Math.max(0, Math.round((now.getTime() - observedAt.getTime()) / 60_000));
  if (minutes === 0) return '0 min';
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest}` : `${hours} h`;
}

function sanitizeMessage(message: string): string {
  const cleaned = message.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim();
  return cleaned || "Data unavailable";
}

function counterLabel(key: string): string {
  return counterLabels[key] ?? key.replace(/_/g, ' ');
}

const counterLabels: Readonly<Record<string, string>> = {
  events_published: "Captured events",
  events_in_target: "Events observed at the destination · current window",
  duplicates_in_target: "Duplicates observed at the destination · current window",
  receiver_rotations: "Journal rotations",
  polls: "Journal reads",
  errors: "Reader errors",
  windows_published: "Published windows",
  load_retries: "Loader recoveries",
  empty_scans: "Empty searches",
  idle_polls: "Empty reads",
  run_duration_s: "Read duration",
  cpu_ms_per_event: "CPU time per event",
  mean_mcpu: "Average CPU",
  payload_bytes_published: "Captured volume",
};
