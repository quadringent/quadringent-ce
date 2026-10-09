import type { EvidenceKind, Overview, Pipeline, PipelineStatus } from './controlPlane.ts';
import { href, statusCopy } from '../router.ts';
import { incidentPublicLabel, stageLabel } from './pipelineDetail.ts';

export interface PipelineFilters {
  readonly name: string;
  readonly status: PipelineStatus | 'all';
  readonly environment: string | 'all';
}

export interface WorkspaceVerdict {
  readonly headline: string;
  readonly detail: string;
  readonly destinationUnverified: number;
  readonly evidenceMode: WorkspaceEvidenceMode;
}
export interface EvidenceCopy { readonly label: string; readonly detail: string; }
export type WorkspaceEvidenceMode = EvidenceKind | 'mixed' | 'unestablished';
export interface WorkspaceFocus {
  readonly pipeline: Pipeline | null;
  readonly actionLabel: string;
  readonly actionHref: string | null;
}

const statusPriority: Readonly<Record<PipelineStatus, number>> = {
  incident: 0,
  awaiting_resume: 1,
  unknown: 2,
  degraded: 3,
  planned_stop: 4,
  recovering: 5,
  healthy: 6,
};

export function prioritizePipelines(pipelines: readonly Pipeline[]): readonly Pipeline[] {
  return [...pipelines].sort((left, right) => {
    const priority = priorityOf(left) - priorityOf(right);
    return priority !== 0 ? priority : left.id.localeCompare(right.id);
  });
}

export function workspaceFocus(overview: Overview): WorkspaceFocus {
  const pipeline = prioritizePipelines(overview.pipelines)[0] ?? null;
  if (!pipeline) {
    return { pipeline: null, actionLabel: "No action available", actionHref: null };
  }
  return {
    pipeline,
    actionLabel: `Inspect ${pipeline.id}`,
    actionHref: href({ name: 'pipeline', id: pipeline.id }),
  };
}

export function filterPipelines(pipelines: readonly Pipeline[], filters: PipelineFilters): readonly Pipeline[] {
  const name = filters.name.trim().toLocaleLowerCase("en-US");
  return prioritizePipelines(pipelines.filter((pipeline) => (
    (!name || pipeline.id.toLocaleLowerCase("en-US").includes(name))
    && (filters.status === 'all' || pipeline.status === filters.status)
    && (filters.environment === 'all' || pipeline.environment === filters.environment)
  )));
}

export function summarizeWorkspace(overview: Overview): WorkspaceVerdict {
  const destinationUnverified = overview.pipelines.filter(hasUnverifiedDestination).length;
  const evidenceMode = workspaceEvidenceMode(overview);
  const incidents = overview.pipelines.filter((pipeline) => pipeline.status === 'incident' || pipeline.incident !== null).length;
  const unknownOrStale = overview.pipelines.filter(isUnknownOrStale).length;
  const unavailableSources = overview.sources.filter((source) => source.status === 'unavailable').length;
  const noSourceAvailable = overview.sources.length > 0 && unavailableSources === overview.sources.length;

  if (overview.pipelines.length === 0 && noSourceAvailable) {
    return { headline: "State unconfirmed: sources unavailable", detail: "No source available: no state can be established.", destinationUnverified, evidenceMode };
  }

  if (incidents > 0) {
    return {
      headline: `${incidents} incident${incidents > 1 ? 's' : ''} ${incidents > 1 ? 'need' : 'needs'} an action`,
      detail: withSourceQualification("Pipelines with incidents take priority over other observations.", unavailableSources),
      destinationUnverified,
      evidenceMode,
    };
  }
  if (destinationUnverified > 0) {
    return {
      headline: `${destinationUnverified} destination${destinationUnverified > 1 ? "s are unverified" : " is unverified"}`,
      detail: withSourceQualification("Arrival at the destination cannot be confirmed from the available information.", unavailableSources),
      destinationUnverified,
      evidenceMode,
    };
  }
  if (unknownOrStale > 0) {
    return {
      headline: `${unknownOrStale} pipeline${unknownOrStale > 1 ? 's restent' : ' reste'} to confirm`,
      detail: withSourceQualification("An unknown state or an old observation cannot be confirmed.", unavailableSources),
      destinationUnverified,
      evidenceMode,
    };
  }
  return {
    headline: evidenceHeadline(evidenceMode),
    detail: evidenceMode === 'live'
      ? withSourceQualification("The current observation reports no issue within its coverage.", unavailableSources)
      : withSourceQualification(evidenceCopy(overview).detail, unavailableSources),
    destinationUnverified,
    evidenceMode,
  };
}

export function isSimulatedOrHistorical(overview: Overview): boolean {
  return workspaceEvidenceMode(overview) !== 'live';
}

export function evidenceCopy(overview: Overview): EvidenceCopy {
  switch (workspaceEvidenceMode(overview)) {
    case 'unestablished': return { label: "Data state unconfirmed", detail: "The received information cannot confirm the data state." };
    case 'simulation': return { label: "Demonstration", detail: "This demonstration data does not represent the actual state." };
    case 'historical': return { label: "Historical observation", detail: "This past observation does not represent the current state." };
    case 'mixed': return { label: "Data with different provenances", detail: "Information with different provenances is mixed; no overall state can be confirmed." };
    case 'live': return { label: "Recent observation", detail: "Observation provided by the service; its date is shown separately." };
  }
}

/** Provenance persistante partagée par les deux surfaces opérateur. */
export function evidenceBannerCopy(overview: Overview): EvidenceCopy {
  return evidenceCopy(overview);
}

export function flowEvidenceClass(pipeline: Pick<Pipeline, 'quality'>): string {
  return `flow-path flow-path--${pipeline.quality.evidenceKind}`;
}

export function flowMotionClass(pipeline: Pipeline): 'flow-path--moving' | 'flow-path--still' {
  return pipeline.quality.evidenceKind === 'live'
    && pipeline.quality.freshness === 'fresh'
    && !hasUnverifiedDestination(pipeline)
    ? 'flow-path--moving'
    : 'flow-path--still';
}

export function flowPathAccessibleLabel(pipeline: Pipeline): string {
  return `Pipeline path ${pipeline.id} : ${pipeline.stages.map((stage) => {
    const label = stageLabel(stage.id);
    if (pipeline.quality.evidenceKind === 'live') return `${label} : ${statusCopy(stage.status).label}`;
    if (pipeline.quality.evidenceKind === 'simulation') {
      return stage.status === 'healthy'
        ? `${label}: observed in the demonstration, actual state unconfirmed`
        : `${label} : ${statusCopy(stage.status).label} — demonstration, actual state unconfirmed`;
    }
    return stage.status === 'healthy'
      ? `${label}: past observation, not a current state`
      : `${label} : ${statusCopy(stage.status).label} — past observation, not a current state`;
  }).join(', ')}`;
}

export function hasUnverifiedDestination(pipeline: Pipeline): boolean {
  const destination = pipeline.stages.find((stage) => stage.id === 'destination');
  return destination?.status === 'unknown' || destination?.observedAt === null;
}

export function isUnknownOrStale(pipeline: Pipeline): boolean {
  return pipeline.status === 'unknown' || pipeline.quality.freshness === 'stale' || pipeline.quality.freshness === 'clock_untrusted';
}

export function pipelineIssue(pipeline: Pipeline): string {
  if (pipeline.status === 'incident') return pipeline.summary;
  if (pipeline.status === 'awaiting_resume') return "Ready to resume";
  if (pipeline.incident) return incidentPublicLabel(pipeline.incident);
  if (hasUnverifiedDestination(pipeline)) return "Destination arrival unconfirmed";
  if (isUnknownOrStale(pipeline)) return "Unknown state or old observation";
  if (pipeline.status === 'degraded') return pipeline.summary;
  if (pipeline.status === 'planned_stop') return "Planned stop";
  if (pipeline.status === 'recovering') return "Recovering";
  return pipeline.quality.evidenceKind === 'simulation' ? "Demonstration: actual state unconfirmed" : "No issue reported";
}

function priorityOf(pipeline: Pipeline): number {
  if (pipeline.status === 'incident' || pipeline.incident !== null) return 0;
  if (isUnknownOrStale(pipeline)) return 1;
  return statusPriority[pipeline.status];
}

function withSourceQualification(detail: string, unavailableSources: number): string {
  return unavailableSources === 0 ? detail : `${detail} Partial coverage: ${unavailableSources} source${unavailableSources > 1 ? 's' : ''} unavailable.`;
}

export function workspaceEvidenceMode(overview: Overview): WorkspaceEvidenceMode {
  if (overview.sources.length === 0) return 'unestablished';
  for (const pipeline of overview.pipelines) {
    const candidates = overview.sources.filter((source) => source.environment === pipeline.environment);
    if (candidates.length === 0) return 'unestablished';
    const candidateKinds = new Set(candidates.map((source) => source.evidenceKind));
    if (candidateKinds.size !== 1 || !candidateKinds.has(pipeline.quality.evidenceKind)) return 'mixed';
  }
  const kinds = new Set<EvidenceKind>([
    ...overview.sources.map((source) => source.evidenceKind),
    ...overview.pipelines.map((pipeline) => pipeline.quality.evidenceKind),
  ]);
  if (kinds.size > 1) return 'mixed';
  return kinds.values().next().value ?? 'unestablished';
}

function evidenceHeadline(mode: WorkspaceEvidenceMode): string {
  switch (mode) {
    case 'unestablished': return "Provenance and state unconfirmed";
    case 'simulation': return "Demonstration available, actual state unconfirmed";
    case 'historical': return "Past observation available, current state unconfirmed";
    case 'mixed': return "Data with different provenances, overall state unconfirmed";
    case 'live': return "No attention required in the current observation";
  }
}
