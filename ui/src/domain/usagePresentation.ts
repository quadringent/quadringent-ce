import type { EvidenceKind, Fleet, FleetPlan, Pipeline } from './controlPlane.ts';
import { buildMeasuredUsage, type MeasuredUsageView } from './usage.ts';

export type UsageGroupId = 'progression' | 'reliability' | 'activity' | 'destination';

export interface UsageGroupView {
  readonly id: UsageGroupId;
  readonly label: string;
  readonly explanation: string;
  readonly metrics: readonly MeasuredUsageView[];
}

export interface PipelineUsagePresentation {
  readonly pipelineId: string;
  readonly environment: string;
  readonly observedAt: string;
  readonly provenance: Pipeline['quality']['evidenceKind'];
  readonly freshness: Pipeline['quality']['freshness'];
  readonly coverage: Pipeline['quality']['coverage'];
  readonly primary: MeasuredUsageView | null;
  readonly groups: readonly UsageGroupView[];
  readonly allMetrics: readonly MeasuredUsageView[];
}

const groupDefinitions: readonly {
  readonly id: UsageGroupId;
  readonly label: string;
  readonly explanation: string;
  readonly keys: readonly string[];
}[] = [
  {
    id: 'progression',
    label: "Progress",
    explanation: "Capture output counters. They do not prove arrival at the destination.",
    keys: ['events_published', 'payload_bytes_published', 'windows_published'],
  },
  {
    id: 'reliability',
    label: "Reliability",
    explanation: "Errors reported by the reader service. Zero remains a measurement, never proof of health.",
    keys: ['errors'],
  },
  {
    id: 'activity',
    label: "Reader activity",
    explanation: "Work observed on the source. A journal rotation is not an error.",
    keys: [
      'polls',
      'idle_polls',
      'empty_scans',
      'receiver_rotations',
      'run_duration_s',
      'mean_mcpu',
      'cpu_ms_per_event',
    ],
  },
  {
    id: 'destination',
    label: "Destination · window",
    explanation:
      "Destination counters over the observed window — not a reconciliation.",
    keys: ['events_in_target', 'duplicates_in_target'],
  },
] as const;

const primaryPriority = ['events_published', 'payload_bytes_published'] as const;

export function buildUsagePresentation(pipelines: readonly Pipeline[]): readonly PipelineUsagePresentation[] {
  return buildMeasuredUsage(pipelines).map((pipeline) => {
    const sourcePipeline = pipelines.find((candidate) => candidate.id === pipeline.pipelineId);
    if (!sourcePipeline) throw new Error(`Telemetry pipeline not found: ${pipeline.pipelineId}`);
    const byKey = new Map(pipeline.metrics.map((metric) => [metric.key, metric]));
    const groups = groupDefinitions.map((definition) => ({
      id: definition.id,
      label: definition.label,
      explanation: definition.explanation,
      metrics: definition.keys.flatMap((key) => {
        const metric = byKey.get(key);
        return metric ? [metric] : [];
      }),
    }));
    const allMetrics = groups.flatMap((group) => group.metrics);
    const primary = primaryPriority
      .map((key) => byKey.get(key))
      .find((metric): metric is MeasuredUsageView => metric !== undefined)
      ?? allMetrics[0]
      ?? null;

    return {
      pipelineId: pipeline.pipelineId,
      environment: pipeline.environment,
      observedAt: pipeline.observedAt,
      provenance: pipeline.provenance,
      freshness: sourcePipeline.quality.freshness,
      coverage: sourcePipeline.quality.coverage,
      primary,
      groups,
      allMetrics,
    };
  });
}

/* ------------------------------------------------------------------ */
/* Estimations dérivées — jamais présentées comme des mesures           */
/* ------------------------------------------------------------------ */

/** Une estimation dérivée d'entrées mesurées, avec sa règle citée et la date
 *  du relevé qui l'alimente. Rendue sous l'étiquette « Estimation » —
 *  « estimation · méthode : {règle} · relevé {date} ». Jamais convertie en
 *  mesure, jamais en zéro quand les entrées manquent. */
export interface DerivedUsageEstimate {
  readonly pipelineId: string | null;
  readonly label: string;
  readonly value: number;
  readonly unit: string;
  /** La règle de calcul citée : « cpu_ms_per_event × événements publiés ». */
  readonly method: string;
  readonly observedAt: string;
  readonly provenance: EvidenceKind | null;
}

/** Estimations calculables à partir des entrées mesurées — CPU cumulé, débit
 *  moyen de publication, crédits restants sur le budget déclaré. Une entrée
 *  absente ne produit aucune estimation : la dimension reste « Non mesuré ». */
export function buildDerivedEstimates(
  pipelines: readonly Pipeline[],
  fleet: { readonly fleet: Fleet; readonly observedAt: string } | null,
): readonly DerivedUsageEstimate[] {
  const estimates: DerivedUsageEstimate[] = [];
  for (const pipeline of pipelines) {
    const events = pipeline.counters.events_published ?? null;
    const cpuPerEvent = pipeline.counters.cpu_ms_per_event ?? null;
    const bytes = pipeline.counters.payload_bytes_published ?? null;
    const duration = pipeline.counters.run_duration_s ?? null;
    if (events !== null && cpuPerEvent !== null) {
      // Unité adaptée à la magnitude : « 18 222 512 ms de CPU » est illisible,
      // « 5,1 h de CPU » se scanne — le calcul servi reste cité dans `method`.
      const cpuMs = cpuPerEvent * events;
      const [cpuValue, cpuUnit] =
        cpuMs >= 3_600_000 ? [cpuMs / 3_600_000, "CPU h"]
        : cpuMs >= 60_000 ? [cpuMs / 60_000, "CPU min"]
        : [cpuMs, "CPU ms"];
      estimates.push({
        pipelineId: pipeline.id,
        label: "Cumulative processing CPU time",
        value: cpuValue,
        unit: cpuUnit,
        method: "CPU time per event × captured events",
        observedAt: pipeline.observedAt,
        provenance: pipeline.quality.evidenceKind,
      });
    }
    if (bytes !== null && duration !== null && duration > 0) {
      const rate = bytes / duration;
      const [rateValue, rateUnit] =
        rate >= 1_048_576 ? [rate / 1_048_576, "MiB / s"]
        : rate >= 1024 ? [rate / 1024, "KiB / s"]
        : [rate, "bytes / s"];
      estimates.push({
        pipelineId: pipeline.id,
        label: "Average publication throughput",
        value: rateValue,
        unit: rateUnit,
        method: "published bytes ÷ processing duration",
        observedAt: pipeline.observedAt,
        provenance: pipeline.quality.evidenceKind,
      });
    }
  }
  if (fleet !== null) {
    estimates.push({
      pipelineId: null,
      label: "Credits remaining in the configured budget",
      value: fleet.fleet.creditBudget - fleet.fleet.consumedCredits - fleet.fleet.reservedCredits,
      unit: "credits",
      method: "budget − consumed − reserved",
      observedAt: fleet.observedAt,
      provenance: null,
    });
  }
  return estimates;
}

/* ------------------------------------------------------------------ */
/* Bornes honnêtes — ce que le relevé ne mesure pas                     */
/* ------------------------------------------------------------------ */

export interface CostBoundary {
  readonly label: string;
  readonly detail: string;
}

/** Liste des dimensions non mesurées — attribution de crédits, coût en
 *  devise, arrivées en destination, compteurs absents. Une dimension mesurée
 *  n'apparaît pas ici ; une dimension absente est dite « non mesurée »,
 *  jamais ramenée à zéro. */
export function costBoundaries(
  pipelines: readonly Pipeline[],
  fleet: Fleet | null,
  fleetPlan: FleetPlan | null,
  hasMeasuredCounters: boolean,
): readonly CostBoundary[] {
  const boundaries: CostBoundary[] = [];
  if (fleet === null) {
    boundaries.push({
      label: "Credit allocation",
      detail: `not measured — ${fleetPlan?.cost.unknownBecause ?? "neither a credit manifest nor a fleet plan is served"}`,
    });
  }
  if (!hasMeasuredCounters) {
    boundaries.push({
      label: "Usage counters",
      detail: "not published for these connections",
    });
  }
  if (!pipelines.some((pipeline) => (pipeline.counters.events_in_target ?? null) !== null)) {
    boundaries.push({
      label: "Destination arrivals",
      detail: "not measured — counter not published",
    });
  }
  boundaries.push({
    label: "Cost in currency",
    detail: "not measured — the service provides neither a cost report nor a rate",
  });
  return boundaries;
}
