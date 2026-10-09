import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { EvidenceKind, Overview, Pipeline, Stage, StageId } from './controlPlane.ts';
import { prioritizePipelines } from './pipelineView.ts';
import { href, inspectHref } from '../router.ts';

export type ProofBreakReason = 'unavailable' | 'incident' | 'unknown' | 'stale' | 'degraded' | 'planned-stop';
export type ProofScopeKind = EvidenceKind | 'mixed' | 'cached' | 'stale' | 'partial' | 'unavailable';

export interface ProofBreak {
  readonly stage: StageId;
  readonly reason: ProofBreakReason;
  readonly cause: string;
}

export interface ProofFocusObject {
  readonly kind: 'break' | 'completion' | 'unavailable';
  readonly stage: StageId | null;
  readonly label: string;
  readonly cause: string;
  readonly evidence: string;
}

export interface ProofDownstream {
  readonly stage: 'destination';
  readonly label: string;
  readonly status: 'not-established' | 'retained-observation';
  readonly detail: string;
  readonly observedAt: string | null;
}

export interface ProofFocus {
  readonly pipelineId: string | null;
  readonly environment: string | null;
  readonly verdict: {
    readonly headline: string;
    readonly detail: string;
  };
  readonly firstBreak: ProofBreak | null;
  readonly upstream: {
    readonly stages: readonly StageId[];
    readonly label: string;
  };
  readonly focus: ProofFocusObject;
  readonly downstream: ProofDownstream | null;
  readonly proofScope: {
    readonly kind: ProofScopeKind;
    readonly label: string;
    readonly detail: string;
  };
  readonly timestamp: {
    readonly value: string | null;
    readonly label: string;
  };
  readonly cta:
    | { readonly kind: 'link'; readonly label: string; readonly href: string }
    | { readonly kind: 'refresh'; readonly label: string };
}

export interface PipelineProofContext {
  readonly generatedAt: string;
  readonly sources: Overview['sources'];
  readonly cached: boolean;
}

const stageOrder = ['source', 'capture', 'raw', 'load', 'destination'] as const;

const stageLabels: Readonly<Record<StageId, string>> = {
  source: 'Source',
  capture: "Reader",
  raw: "Reception area",
  load: "Initial copy",
  destination: 'Destination',
};

const expectedEvidence: Readonly<Record<StageId, string>> = {
  source: "Source availability",
  capture: "Reader recovery",
  raw: "Durable receipt",
  load: "Initial copy",
  destination: "Confirmed arrival",
};

export function resolveProofFocus(state: ControlPlaneState): ProofFocus {
  if (state.status === 'loading' || state.status === 'failed') {
    return unavailableFocus(state.status === 'loading');
  }

  const pipeline = prioritizePipelines(state.overview.pipelines)[0] ?? null;
  if (!pipeline) return emptyFocus(state.overview, isCachedState(state));

  return resolvePipelineProofFocus(pipeline, {
    generatedAt: state.overview.generatedAt,
    sources: state.overview.sources,
    cached: isCachedState(state),
  });
}

export function resolvePipelineProofFocus(pipeline: Pipeline, context: PipelineProofContext): ProofFocus {
  const { cached } = context;
  const sourceAvailability = pipelineSourceAvailability(context.sources, pipeline);
  const firstBreak = resolveFirstBreak(sourceAvailability, pipeline);
  const proofScope = resolveProofScope(pipeline, cached, sourceAvailability);
  const timestamp = {
    value: pipeline.observedAt,
    label: cached ? "Last retained observation" : "Observation time",
  };

  if (!firstBreak) {
    return completionFocus(pipeline, proofScope, timestamp, cached);
  }

  const breakIndex = stageOrder.indexOf(firstBreak.stage);
  const upstreamStages = stageOrder.slice(0, breakIndex);
  const focus: ProofFocusObject = {
    kind: 'break',
    stage: firstBreak.stage,
    label: stageLabels[firstBreak.stage],
    cause: firstBreak.cause,
    evidence: `Expected · ${expectedEvidence[firstBreak.stage]}`,
  };

  return {
    pipelineId: pipeline.id,
    environment: pipeline.environment,
    verdict: {
      headline: cached ? "Latest delivery unconfirmed." : "Delivery unconfirmed.",
      detail: `First unconfirmed point: ${stageReference(firstBreak.stage)}.`,
    },
    firstBreak,
    upstream: {
      stages: upstreamStages,
      label: upstreamStages.map((stage) => stageLabels[stage]).join(' · '),
    },
    focus,
    downstream: downstreamAfterBreak(pipeline, firstBreak.stage),
    proofScope,
    timestamp,
    cta: {
      kind: 'link',
      label: `Inspect ${stageReference(firstBreak.stage)}`,
      href: inspectHref(pipeline.id, firstBreak.stage),
    },
  };
}

type PipelineSourceAvailability = 'available' | 'partial' | 'unavailable' | 'unmapped' | 'mismatch';

function resolveFirstBreak(sourceAvailability: PipelineSourceAvailability, pipeline: Pipeline): ProofBreak | null {
  if (sourceAvailability === 'unavailable') {
    return {
      stage: 'source',
      reason: 'unavailable',
      cause: "The source is unavailable. The last observation remains available.",
    };
  }
  if (sourceAvailability === 'partial') {
    return {
      stage: 'source',
      reason: 'unavailable',
      cause: "Some sources are unavailable; arrival cannot be confirmed.",
    };
  }
  if (sourceAvailability === 'unmapped') {
    return {
      stage: 'source',
      reason: 'unavailable',
      cause: "No source is linked to this environment; availability is not established.",
    };
  }
  if (sourceAvailability === 'mismatch') {
    return {
      stage: 'source',
      reason: 'unavailable',
      cause: "Source provenance is inconsistent or mixed; arrival cannot be confirmed.",
    };
  }

  if (pipeline.quality.freshness === 'stale') {
    return {
      stage: 'source',
      reason: 'stale',
      cause: "The latest observation is too old.",
    };
  }
  if (pipeline.quality.freshness === 'clock_untrusted') {
    return {
      stage: 'source',
      reason: 'stale',
      cause: "The observation time is unreliable.",
    };
  }

  let stageCandidate: ProofBreak | null = null;
  for (const stageId of stageOrder) {
    const stage = pipeline.stages.find((candidate) => candidate.id === stageId);
    if (!stage) {
      stageCandidate = unknownStageBreak(stageId);
      break;
    }
    const broken = stageBreak(stage);
    if (broken) {
      stageCandidate = broken;
      break;
    }
  }

  const qualificationCandidate = pipelineQualificationBreak(pipeline);
  if (!stageCandidate) return qualificationCandidate;
  if (!qualificationCandidate) return stageCandidate;
  return stageOrder.indexOf(stageCandidate.stage) <= stageOrder.indexOf(qualificationCandidate.stage)
    ? stageCandidate
    : qualificationCandidate;
}

function pipelineQualificationBreak(pipeline: Pipeline): ProofBreak | null {
  if (pipeline.incident) {
    const destinationIncident = pipeline.incident.type === 'destination';
    return {
      stage: destinationIncident ? 'destination' : 'capture',
      reason: 'incident',
      cause: destinationIncident
        ? "A destination incident is reported; this observation cannot establish its nature."
        : "A reader incident is reported; this observation cannot establish its nature.",
    };
  }
  if (pipeline.status === 'incident') {
    return {
      stage: 'destination',
      reason: 'incident',
      cause: "A global incident is reported; delivery is unconfirmed.",
    };
  }
  if (pipeline.quality.freshness === 'late') {
    return {
      stage: 'destination',
      reason: 'degraded',
      cause: "The observation is late; delivery is unconfirmed.",
    };
  }
  if (pipeline.quality.coverage !== 'complete') {
    return {
      stage: 'destination',
      reason: 'unknown',
      cause: "The observation is incomplete; delivery is unconfirmed.",
    };
  }
  if (pipeline.status === 'recovering') {
    return {
      stage: 'destination',
      reason: 'degraded',
      cause: "Recovery is in progress; delivery is not yet confirmed.",
    };
  }
  if (pipeline.status === 'unknown') {
    return {
      stage: 'destination',
      reason: 'unknown',
      cause: "The pipeline state is unknown; delivery is unconfirmed.",
    };
  }
  if (pipeline.status === 'planned_stop') {
    return {
      stage: 'destination',
      reason: 'planned-stop',
      cause: "The pipeline has a planned pause; current delivery is unconfirmed.",
    };
  }
  if (pipeline.status === 'awaiting_resume') {
    return {
      stage: 'capture',
      reason: 'planned-stop',
      cause: "The stop cause is resolved; capture is waiting for restart.",
    };
  }
  if (pipeline.status === 'degraded' && pipeline.quality.evidenceKind === 'live') {
    return {
      stage: 'destination',
      reason: 'degraded',
      cause: "The overall state is degraded; delivery is unconfirmed.",
    };
  }
  return null;
}

function stageBreak(stage: Stage): ProofBreak | null {
  if (stage.status === 'incident') {
    return { stage: stage.id, reason: 'incident', cause: stage.detail };
  }
  if (stage.status === 'degraded') {
    return { stage: stage.id, reason: 'degraded', cause: stage.detail };
  }
  if (stage.status === 'planned_stop') {
    return { stage: stage.id, reason: 'planned-stop', cause: "This stage has a planned pause." };
  }
  if (stage.status === 'awaiting_resume') {
    return { stage: stage.id, reason: 'planned-stop', cause: "This stage is waiting for restart." };
  }
  if (stage.status === 'unknown' || stage.observedAt === null) return unknownStageBreak(stage.id);
  return null;
}

function unknownStageBreak(stage: StageId): ProofBreak {
  const cause: Readonly<Record<StageId, string>> = {
    source: "No source observation has been received.",
    capture: "No reader recovery has been observed.",
    raw: "No durable receipt has been observed.",
    load: "No initial copy has been confirmed.",
    destination: "No arrival has been confirmed.",
  };
  return { stage, reason: 'unknown', cause: cause[stage] };
}

function downstreamAfterBreak(pipeline: Pipeline, breakStage: StageId): ProofDownstream | null {
  if (breakStage === 'destination') return null;
  const destination = pipeline.stages.find((stage) => stage.id === 'destination');
  if (destination?.status === 'healthy' && destination.observedAt !== null) {
    return {
      stage: 'destination',
      label: 'Destination',
      status: 'retained-observation',
      detail: "Last retained observation; it does not confirm delivery beyond the first unconfirmed point.",
      observedAt: destination.observedAt,
    };
  }
  return {
    stage: 'destination',
    label: 'Destination',
    status: 'not-established',
    detail: "Unconfirmed in the current observation.",
    observedAt: destination?.observedAt ?? null,
  };
}

function completionFocus(
  pipeline: Pipeline,
  proofScope: ProofFocus['proofScope'],
  timestamp: ProofFocus['timestamp'],
  cached: boolean,
): ProofFocus {
  const copy = completionCopy(pipeline.quality.evidenceKind, cached);
  return {
    pipelineId: pipeline.id,
    environment: pipeline.environment,
    verdict: copy,
    firstBreak: null,
    upstream: {
      stages: ['source', 'capture', 'raw', 'load'],
      label: "Source · Reader · Reception area · Initial copy",
    },
    focus: {
      kind: 'completion',
      stage: 'destination',
      label: 'Destination',
      cause: completionCause(pipeline.quality.evidenceKind, cached),
      evidence: "Receipt · Confirmed arrival",
    },
    downstream: null,
    proofScope,
    timestamp,
    cta: {
      kind: 'link',
      label: "Inspect delivery",
      href: href({ name: 'pipeline', id: pipeline.id }),
    },
  };
}

function completionCopy(kind: EvidenceKind, cached: boolean): ProofFocus['verdict'] {
  if (cached) {
    return {
      headline: "Latest delivery confirmed in the retained observation.",
      detail: "This retained observation does not confirm the current state.",
    };
  }
  switch (kind) {
    case 'simulation':
      return {
        headline: "Delivery confirmed in the demonstration.",
        detail: "This demonstration does not confirm the actual state.",
      };
    case 'historical':
      return {
        headline: "Delivery confirmed in an earlier observation.",
        detail: "This earlier observation does not confirm the current state.",
      };
    case 'live':
      return {
        headline: "Delivery confirmed in this observation.",
        detail: "All five stages are observed, recent and consistent.",
      };
  }
}

function completionCause(kind: EvidenceKind, cached: boolean): string {
  if (cached) return "The latest confirmed arrival belongs to the retained observation.";
  if (kind === 'simulation') return "The destination is observed only in the demonstration.";
  if (kind === 'historical') return "The destination is observed only in the earlier observation.";
  return "Arrival is confirmed in the current observation.";
}

function resolveProofScope(
  pipeline: Pipeline,
  cached: boolean,
  sourceAvailability: PipelineSourceAvailability,
): ProofFocus['proofScope'] {
  if (cached) {
    return {
      kind: 'cached',
      label: "Retained observation · offline",
      detail: "The last known composition is retained without presenting it as current.",
    };
  }
  if (sourceAvailability === 'unavailable') {
    return {
      kind: 'unavailable',
      label: "Sources unavailable",
      detail: "No source in this scope can confirm the current observation.",
    };
  }
  if (sourceAvailability === 'partial') {
    return {
      kind: 'partial',
      label: "Some sources unavailable",
      detail: "Source coverage is ambiguous; no current delivery is confirmed.",
    };
  }
  if (sourceAvailability === 'unmapped') {
    return {
      kind: 'unavailable',
      label: "Source availability not established",
      detail: "No source is linked to this environment; no current delivery is confirmed.",
    };
  }
  if (sourceAvailability === 'mismatch') {
    return {
      kind: 'mixed',
      label: "Mixed or inconsistent source provenance",
      detail: "Source provenance does not clearly match the pipeline; no current delivery is confirmed.",
    };
  }
  if (pipeline.quality.freshness === 'stale' || pipeline.quality.freshness === 'clock_untrusted') {
    return {
      kind: 'stale',
      label: scopedBoundaryLabel(pipeline, pipeline.quality.freshness === 'stale' ? "Observation too old" : "Clock unreliable"),
      detail: `${nonLiveBoundary(pipeline)}The observation is not recent enough to confirm the current state.`,
    };
  }
  if (pipeline.quality.freshness === 'late') {
    return {
      kind: 'stale',
      label: scopedBoundaryLabel(pipeline, "Late observation"),
      detail: `${nonLiveBoundary(pipeline)}The observation is not recent enough to confirm the current state.`,
    };
  }
  if (pipeline.quality.coverage !== 'complete') {
    return {
      kind: 'partial',
      label: scopedBoundaryLabel(pipeline, "Incomplete observation"),
      detail: `${nonLiveBoundary(pipeline)}The observation does not cover every stage; delivery is unconfirmed.`,
    };
  }
  switch (pipeline.quality.evidenceKind) {
    case 'simulation': return { kind: 'simulation', label: "Demonstration", detail: "This demonstration does not represent the actual state." };
    case 'historical': return { kind: 'historical', label: "Earlier observation", detail: "This earlier observation does not represent the current state." };
    case 'live': return { kind: 'live', label: "Actual system", detail: "Observation reported by the service; freshness is shown separately." };
  }
}

function scopedBoundaryLabel(pipeline: Pipeline, boundary: string): string {
  if (pipeline.quality.evidenceKind === 'simulation') return `Demonstration · ${boundary.toLocaleLowerCase("en-US")}`;
  if (pipeline.quality.evidenceKind === 'historical') return `Earlier observation · ${boundary.toLocaleLowerCase("en-US")}`;
  return boundary;
}

function nonLiveBoundary(pipeline: Pipeline): string {
  if (pipeline.quality.evidenceKind === 'simulation') return "This demonstration does not represent the actual state. ";
  if (pipeline.quality.evidenceKind === 'historical') return "This earlier observation does not represent the current state. ";
  return '';
}

function unavailableFocus(loading: boolean): ProofFocus {
  return {
    pipelineId: null,
    environment: null,
    verdict: {
      headline: loading ? "Reading the observation." : "Delivery state unavailable.",
      detail: loading
        ? "No verdict is calculated before the first observation is received."
        : "No usable observation is available. Neither health nor a break is inferred.",
    },
    firstBreak: null,
    upstream: { stages: [], label: "No upstream stage observed" },
    focus: {
      kind: 'unavailable',
      stage: null,
      label: "Stage unavailable",
      cause: loading ? "Waiting for the first observation." : "The service provides no usable observation.",
      evidence: "Expected · Service observation",
    },
    downstream: {
      stage: 'destination',
      label: 'Destination',
      status: 'not-established',
      detail: "Unconfirmed without an observation.",
      observedAt: null,
    },
    proofScope: {
      kind: 'unavailable',
      label: "Evidence not established",
      detail: "No observation retained in this session.",
    },
    timestamp: { value: null, label: "No observation" },
    cta: { kind: 'refresh', label: loading ? "Restart the reader" : "Refresh" },
  };
}

function emptyFocus(overview: Overview, cached: boolean): ProofFocus {
  const unavailable = unavailableFocus(false);
  const evidenceScope = emptyEvidenceScope(overview);
  const inventoryEstablished = !cached
    && overview.sources.length > 0
    && overview.sources.every((source) => source.status === 'available');
  return {
    ...unavailable,
    verdict: {
      headline: inventoryEstablished ? "No pipeline observed." : "Pipeline presence not established.",
      detail: inventoryEstablished
        ? "The absence of a pipeline does not prove delivery."
        : "The available observation cannot confirm pipeline presence.",
    },
    focus: {
      ...unavailable.focus,
      label: inventoryEstablished ? "No stage to inspect" : "Inventory not established",
      cause: inventoryEstablished
        ? "The observation contains no pipeline."
        : "The sources cannot establish the current inventory.",
    },
    proofScope: cached
      ? { kind: 'cached', label: "Retained observation · offline", detail: "Retained inventory, not current." }
      : evidenceScope,
    timestamp: { value: overview.generatedAt, label: cached ? "Last retained observation" : "Observation time" },
  };
}

function emptyEvidenceScope(overview: Overview): ProofFocus['proofScope'] {
  if (overview.sources.length === 0) {
    return {
      kind: 'unavailable',
      label: "Evidence not established",
      detail: "No source can confirm observation coverage.",
    };
  }
  const availableSources = overview.sources.filter((source) => source.status === 'available').length;
  if (availableSources === 0) {
    return {
      kind: 'unavailable',
      label: "Sources unavailable",
      detail: "No source can establish a current inventory.",
    };
  }
  if (availableSources !== overview.sources.length) {
    return {
      kind: 'partial',
      label: "Some sources unavailable",
      detail: "Source coverage cannot establish a complete inventory.",
    };
  }
  const kinds = new Set(overview.sources.map((source) => source.evidenceKind));
  if (kinds.size > 1) {
    return {
      kind: 'mixed',
      label: "Mixed provenances",
      detail: "Different observation provenances coexist; they do not confirm an overall actual state.",
    };
  }
  switch (kinds.values().next().value as EvidenceKind | undefined) {
    case 'simulation': return { kind: 'simulation', label: "Demonstration", detail: "This demonstration does not represent the actual state." };
    case 'historical': return { kind: 'historical', label: "Earlier observation", detail: "This earlier observation does not represent the current state." };
    case 'live': return { kind: 'live', label: "Actual system", detail: "Observation reported by the service; freshness is shown separately." };
    default: return { kind: 'unavailable', label: "Evidence not established", detail: "No delivery chain is visible." };
  }
}

function pipelineSourceAvailability(sources: Overview['sources'], pipeline: Pipeline): PipelineSourceAvailability {
  const candidates = sources.filter((source) => source.environment === pipeline.environment);
  if (candidates.length === 0) return 'unmapped';
  const availableCount = candidates.filter((source) => source.status === 'available').length;
  if (availableCount === 0) return 'unavailable';
  if (availableCount !== candidates.length) return 'partial';
  const candidateKinds = new Set(candidates.map((source) => source.evidenceKind));
  if (candidateKinds.size !== 1 || !candidateKinds.has(pipeline.quality.evidenceKind)) return 'mismatch';
  return 'available';
}

function isCachedState(state: Exclude<ControlPlaneState, { status: 'loading' | 'failed' }>): boolean {
  return state.status === 'degraded' || state.connection === 'offline' || state.connection === 'reconnecting';
}

function stageReference(stage: StageId): string {
  switch (stage) {
    case 'source': return "the source";
    case 'capture': return "the reader";
    case 'raw': return "reception";
    case 'load': return "the initial copy";
    case 'destination': return "the destination";
  }
}
