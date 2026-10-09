import type { EvidenceKind, Pipeline } from './controlPlane.ts';

interface MeasuredUsageInput {
  readonly kind: 'measured';
  readonly key: string;
  readonly label: string;
  readonly value: number;
  readonly unit: string;
  readonly source: string;
  readonly observedAt: string;
  readonly provenance: EvidenceKind;
}

interface EstimateUsageInput {
  readonly kind: 'estimate';
  readonly label: string;
  readonly value: number;
  readonly currency: string;
  readonly methodology: string;
  readonly assumptions: readonly string[];
  readonly asOf: string;
  readonly region: string;
}

export interface MeasuredUsageView extends MeasuredUsageInput {
  readonly badge: "Measured";
}

export interface EstimateUsageView extends EstimateUsageInput {
  readonly badge: "Estimate";
  readonly unit: string;
}

export type UsageView = MeasuredUsageView | EstimateUsageView;

export interface PipelineUsageView {
  readonly pipelineId: string;
  readonly environment: string;
  readonly observedAt: string;
  readonly provenance: EvidenceKind;
  readonly metrics: readonly MeasuredUsageView[];
}

interface CounterDefinition {
  readonly label: string;
  readonly unit: string;
}

const counterDefinitions: Readonly<Record<string, CounterDefinition>> = {
  events_published: { label: "Captured events", unit: "events" },
  payload_bytes_published: { label: "Captured volume", unit: "bytes" },
  cpu_ms_per_event: { label: "CPU time per event", unit: "ms / event" },
  mean_mcpu: { label: "Average CPU", unit: "core" },
  duplicates_in_target: { label: "Duplicates observed at the destination", unit: "duplicates (window)" },
  empty_scans: { label: "Empty searches", unit: 'recherches' },
  errors: { label: "Reader errors", unit: 'errors' },
  events_in_target: { label: "Events observed at the destination", unit: "events (window)" },
  idle_polls: { label: "Empty reads", unit: "reads" },
  polls: { label: "Journal reads", unit: "reads" },
  receiver_rotations: { label: "Journal rotations", unit: 'rotations' },
  run_duration_s: { label: "Read duration", unit: 'secondes' },
  windows_published: { label: "Published windows", unit: "windows" },
};

export function usageView(input: MeasuredUsageInput): MeasuredUsageView;
export function usageView(input: EstimateUsageInput): EstimateUsageView;
export function usageView(input: MeasuredUsageInput | EstimateUsageInput): UsageView {
  if (!Number.isFinite(input.value)) throw new Error("Usage value must be finite");
  if (input.kind === 'measured') {
    if (!input.unit.trim() || !input.source.trim() || !input.observedAt.trim() || !input.provenance) {
      throw new Error("A measurement requires a unit, source, observation and provenance");
    }
    return { ...input, badge: "Measured" };
  }

  if (
    !input.methodology.trim()
    || !input.assumptions.length
    || input.assumptions.some((assumption) => !assumption.trim())
    || !input.asOf.trim()
    || !input.region.trim()
  ) {
    throw new Error("An estimate requires a methodology, assumption, date and region");
  }
  return { ...input, badge: "Estimate", unit: input.currency };
}

export function buildMeasuredUsage(pipelines: readonly Pipeline[]): readonly PipelineUsageView[] {
  return pipelines.map((pipeline) => ({
    pipelineId: pipeline.id,
    environment: pipeline.environment,
    observedAt: pipeline.observedAt,
    provenance: pipeline.quality.evidenceKind,
    metrics: Object.entries(pipeline.counters).flatMap(([key, value]) => {
      const definition = counterDefinitions[key];
      if (definition === undefined || value === null) return [];
      return [usageView({
        kind: 'measured',
        key,
        label: definition.label,
        value,
        unit: definition.unit,
        source: `control plane / ${pipeline.id} / counters.${key}`,
        observedAt: pipeline.observedAt,
        provenance: pipeline.quality.evidenceKind,
      })];
    }),
  }));
}
