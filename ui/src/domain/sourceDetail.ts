import type {
  FluxIdentity,
  JournalCheckpoint,
  JournalPosition,
  Pipeline,
  RunProjection,
  Stage,
  StageId,
} from './controlPlane.ts';
import { age, formatDuration, position, sequences, timestamp } from './format.ts';
import type { BoardTone } from './liveBoard.ts';
import { resumeReadiness } from './liveBoard.ts';
import { stageLabel, stageStatusLabel } from './pipelineDetail.ts';

/**
 * View-model du détail Source (L1/L2/L3) — extension S5 null-tolérante.
 *
 * Trois cas sont distingués partout, jamais fusionnés :
 * - extension absente du relevé (champ undefined/null) → « Non instrumenté » ;
 * - bloc servi mais membre absent → « non mesuré » / « inconnu » ;
 * - valeur mesurée → affichée telle quelle, jamais arrondie ni verdée.
 *
 * Le seul vert de la page est `apply HH:MM:SS` : la seule preuve mesurée de
 * livraison destination. Un checkpoint ou un compteur n'est pas une arrivée.
 */

export const NOT_INSTRUMENTED = "Not measured";

const clockUtcFormat = new Intl.DateTimeFormat("en-US", { hourCycle: 'h23',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  timeZone: 'UTC',
});

/** HH:MM:SS UTC — l'horodatage de preuve, jamais une heure locale. */
export function clockUtc(iso: string): string {
  return clockUtcFormat.format(new Date(iso));
}

export function checkpointGlyph(checkpoint: JournalCheckpoint): string {
  return `${checkpoint.receiver}:${sequences(checkpoint.sequence)}`;
}

/* ------------------------------------------------------------------ */
/* Glyphe position journal — checkpoint → tail + fenêtre receiver       */
/* ------------------------------------------------------------------ */

export interface PositionGlyph {
  /** `false` = extension S5 absente du relevé (non instrumenté). */
  readonly instrumented: boolean;
  /** Forme canonique : `receiver:seq → receiver:seq`. */
  readonly value: string;
  /** Fenêtre du receiver actif quand elle est publiée, sinon null. */
  readonly window: string | null;
  /** Pourquoi la position est incomplète — jamais masquée. */
  readonly reason: string | null;
}

export function positionGlyph(pipeline: Pipeline): PositionGlyph {
  const projected = pipeline.position ?? null;
  if (projected === null) {
    // Avant S5, le checkpoint runtime reste une mesure servie ; le bloc de
    // reprise ou la sonde catalogue peuvent déjà servir le tail courant.
    const runtime = pipeline.fleetRuntime?.checkpoint ?? pipeline.resume?.checkpoint ?? null;
    const tail = pipeline.resume?.tail ?? pipeline.fleetPlan?.cutoverCheckpoint ?? null;
    if (runtime !== null && tail !== null) {
      return {
        instrumented: true,
        value: `${checkpointGlyph(runtime)} → ${checkpointGlyph(tail)}`,
        window: null,
        reason: "journal end observed continuously",
      };
    }
    if (runtime !== null) {
      return {
        instrumented: false,
        value: `${checkpointGlyph(runtime)} → end unknown`,
        window: null,
        reason: "journal end not measured — measurement not published by the service",
      };
    }
    return {
      instrumented: false,
      value: NOT_INSTRUMENTED,
      window: null,
      reason: "journal position not measured — measurement not published by the service",
    };
  }
  const window = receiverWindow(projected);
  const checkpoint = projected.checkpoint;
  // Le bloc resume porte la fin du journal mesurée par la sonde catalogue —
  // plus fraîche que le tail du relevé figé quand la capture est parquée.
  const tail = pipeline.resume?.tail ?? projected.sourceTail;
  if (checkpoint === null && tail === null) {
    return {
      instrumented: true,
      value: "Unknown — boundaries not published",
      window,
      reason: "checkpoint and journal end absent from the observation",
    };
  }
  return {
    instrumented: true,
    value: `${checkpoint === null ? 'inconnu' : checkpointGlyph(checkpoint)} → ${tail === null ? "end unknown" : checkpointGlyph(tail)}`,
    window,
    reason: checkpoint === null || tail === null ? "boundary missing from the observation" : null,
  };
}

function receiverWindow(position: JournalPosition): string | null {
  const first = position.receiverFirstSequence;
  const last = position.receiverLastSequence;
  if (first === null && last === null) return null;
  const low = first === null ? 'inconnue' : sequences(first);
  const high = last === null ? 'inconnue' : sequences(last);
  return `sequences ${low} → ${high}`;
}

/** Lignes L3 de la position — même distinction instrumenté/mesuré. */
export function positionRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const projected = pipeline.position ?? null;
  if (projected === null) {
    const runtime = pipeline.fleetRuntime?.checkpoint ?? pipeline.resume?.checkpoint ?? null;
    const resumeTail = pipeline.resume?.tail ?? pipeline.fleetPlan?.cutoverCheckpoint ?? null;
    return [
      ["Reader checkpoint", runtime === null ? "Unknown — execution not projected" : `${position(runtime.receiver, runtime.sequence)} (runtime)`],
      ["Journal end", resumeTail === null ? "Not measured — measurement not published by the service" : `${position(resumeTail.receiver, resumeTail.sequence)} (continuous observation)`],
      ["Receiver sequences", "Not measured — measurement not published by the service"],
    ];
  }
  const tail = pipeline.resume?.tail ?? projected.sourceTail;
  return [
    ["Reader checkpoint", projected.checkpoint === null ? "Unknown — not published" : position(projected.checkpoint.receiver, projected.checkpoint.sequence)],
    ["Journal end", tail === null ? "Unknown — not published" : position(tail.receiver, tail.sequence)],
    ["First receiver sequence", projected.receiverFirstSequence === null ? "Unknown — not published" : sequences(projected.receiverFirstSequence)],
    ["Last receiver sequence", projected.receiverLastSequence === null ? "Unknown — not published" : sequences(projected.receiverLastSequence)],
  ];
}

/* ------------------------------------------------------------------ */
/* Tampon apply — la seule preuve verte de livraison                    */
/* ------------------------------------------------------------------ */

export interface ApplyStamp {
  /** `apply 09:42:13` quand `destination.observed_at` est mesuré. */
  readonly label: string;
  readonly measured: boolean;
  readonly at: string | null;
  readonly reason: string | null;
}

const DESTINATION_REASON_LABELS: Readonly<Record<string, string>> = {
  destination_not_attached: "destination not linked to the observation",
  destination_invalid: 'bloc destination illisible',
};

export function destinationReasonLabel(reason: string | null | undefined): string {
  if (reason === undefined || reason === null) return "reason not configured";
  return DESTINATION_REASON_LABELS[reason] ?? reason;
}

export function destinationApplyStamp(pipeline: Pipeline): ApplyStamp {
  const destination = pipeline.destination ?? null;
  if (destination !== null && destination.observedAt !== null) {
    return {
      label: `delivered ${clockUtc(destination.observedAt)}`,
      measured: true,
      at: destination.observedAt,
      reason: null,
    };
  }
  if (destination !== null) {
    return {
      label: "date not observed",
      measured: false,
      at: null,
      reason: "destination observation served without an application date",
    };
  }
  if (pipeline.destination === null) {
    return {
      label: "date not served",
      measured: false,
      at: null,
      reason: destinationReasonLabel(pipeline.destinationReason),
    };
  }
  return {
    label: "date not served",
    measured: false,
    at: null,
    reason: "measurement not published by the service",
  };
}

/** Watermark L1 : apply mesuré si S5 le sert, sinon le compteur de fenêtre. */
export function destinationStamp(pipeline: Pipeline, current: boolean): {
  readonly value: string;
  readonly hint: string | null;
  readonly tone: BoardTone;
} {
  const stamp = destinationApplyStamp(pipeline);
  if (stamp.measured) {
    const namespace = pipeline.fleetPlan?.destinationNamespace ?? pipeline.fleet?.destinationNamespace ?? null;
    return {
      value: stamp.label,
      hint: namespace === null ? 'destination observation' : `${namespace} · destination observation`,
      tone: current ? 'positive' : 'muted',
    };
  }
  const inTarget = pipeline.counters.events_in_target ?? null;
  const namespace = pipeline.fleetPlan?.destinationNamespace ?? pipeline.fleet?.destinationNamespace ?? null;
  if (inTarget !== null) {
    return {
      value: `${sequences(inTarget)} event${inTarget > 1 ? 's' : ''} observed`,
      hint: `${stamp.label} · ${namespace === null ? "observed total — not a reconciliation" : `${namespace} · observed total — not a reconciliation`}`,
      tone: 'active',
    };
  }
  if (namespace !== null) {
    return {
      value: namespace,
      hint: `${stamp.label} · ${stamp.reason ?? "delivery counter not served"}`,
      tone: 'muted',
    };
  }
  return { value: "Destination · undated", hint: stamp.reason, tone: 'muted' };
}

/* ------------------------------------------------------------------ */
/* Chaîne des cinq étapes — preuves, âges, apply = seul vert            */
/* ------------------------------------------------------------------ */

export interface StageChainStep {
  readonly id: StageId;
  readonly label: string;
  readonly statusLabel: string;
  /** Ce que l'étape prouve — headline projetée, jamais reformulée. */
  readonly evidence: string;
  readonly detail: string;
  readonly observedAt: string | null;
  readonly ageLabel: string;
  /** `apply HH:MM:SS` — seule la destination le porte, seul vert mesuré. */
  readonly apply: string | null;
  readonly tone: BoardTone;
}

export function stageChain(pipeline: Pipeline, now: Date, current: boolean): readonly StageChainStep[] {
  const stamp = destinationApplyStamp(pipeline);
  const ready = resumeReadiness(pipeline) === 'ready';
  return pipeline.stages.map((stage) => chainStep(stage, now, current, stamp, ready));
}

function chainStep(stage: Stage, now: Date, current: boolean, stamp: ApplyStamp, ready: boolean): StageChainStep {
  const apply = stage.id === 'destination' && stamp.measured ? stamp.label : null;
  const ageLabel = stage.observedAt === null ? "never observed" : `observed ${age(stage.observedAt, now).label}`;
  let tone: BoardTone;
  if (apply !== null) {
    tone = current ? 'positive' : 'muted';
  } else if (stage.status === 'incident' || stage.status === 'degraded') {
    tone = 'attention';
  } else if (stage.status === 'healthy' && stage.observedAt !== null) {
    tone = current ? 'active' : 'muted';
  } else {
    tone = 'muted';
  }
  return {
    id: stage.id,
    label: stageLabel(stage.id),
    // « Disponible » affirme une disponibilité courante — sur un relevé figé,
    // l'étape saine n'est qu'observée, pas prouvée à l'instant présent.
    // Une capture arrêtée dont la cause est résolue attend le relancement.
    statusLabel: ready && stage.id === 'capture' && (stage.status === 'incident' || stage.status === 'awaiting_resume')
      ? "To resume"
      : !current && stage.status === 'healthy'
        ? "Observed"
        : stageStatusLabel(stage.status),
    // Quand le statut porte déjà « À reprendre », la preuve montre le détail.
    evidence: ready && stage.id === 'capture' && (stage.status === 'incident' || stage.status === 'awaiting_resume')
      ? stage.detail
      : stage.headline,
    detail: stage.detail,
    observedAt: stage.observedAt,
    ageLabel,
    apply,
    tone,
  };
}

/* ------------------------------------------------------------------ */
/* Verdict lag projeté — déclaré par le worker, jamais déduit           */
/* ------------------------------------------------------------------ */

export interface ProjectedLagVerdict {
  readonly label: string;
  /** Raison quand le verdict n'est pas mesuré. */
  readonly reason: string | null;
  readonly instrumented: boolean;
}

const PROJECTED_LAG_VERDICTS: Readonly<Record<string, string>> = {
  STABLE: 'Stable',
  BOUNDED: "Bounded",
  CATCHING_UP: "Catching up",
  DIVERGING: 'Divergence',
};

const LAG_VERDICT_REASONS: Readonly<Record<string, string>> = {
  verdict_not_declared: "verdict not reported by the worker",
  verdict_out_of_contract: 'verdict outside contract',
};

export function projectedLagVerdict(pipeline: Pipeline): ProjectedLagVerdict {
  if (pipeline.lagVerdict === undefined && pipeline.lagVerdictReason === undefined) {
    return { label: NOT_INSTRUMENTED, reason: "measurement not published by the service", instrumented: false };
  }
  const verdict = pipeline.lagVerdict ?? null;
  if (verdict !== null) {
    return { label: PROJECTED_LAG_VERDICTS[verdict] ?? verdict, reason: null, instrumented: true };
  }
  const reason = pipeline.lagVerdictReason ?? null;
  return {
    label: "Undetermined",
    reason: reason === null ? "reason not configured" : (LAG_VERDICT_REASONS[reason] ?? reason),
    instrumented: true,
  };
}

/* ------------------------------------------------------------------ */
/* Exécution projetée — run.state, démarrage, diagnostic, pause         */
/* ------------------------------------------------------------------ */

const RUN_STATE_LABELS: Readonly<Record<string, string>> = {
  RUNNING: "In progress",
  PAUSED_SOURCE: "Source pause",
  STOPPED_BUDGET: "Budget stop",
  STOPPED_PROOF_CHAIN: "Stopped — windows closed",
  STOPPED_FAIL_CLOSED: "Stopped safely",
  STOPPED_AUTH_BLOCKED: "Authentication blocked",
};

export function runStateLabel(state: string | null): string {
  if (state === null) return "Unknown — state not published";
  return RUN_STATE_LABELS[state] ?? state;
}

/** Lignes L3 du run — présentes même quand le bloc n'est pas instrumenté. */
export function runDetailRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const run = pipeline.run ?? null;
  if (run === null) {
    return [
      ["Run state", "Not measured — measurement not published by the service"],
      ["Start", NOT_INSTRUMENTED],
      ["Measured duration", "Not measured"],
      ["Stop reason", "Not measured"],
      ["Last diagnostic", NOT_INSTRUMENTED],
      ["Source pause", "Not measured"],
    ];
  }
  return [
    ["Run state", runStateLabel(run.state)],
    ["Start", run.startedAt === null ? "Unknown — not measured" : `${timestamp(run.startedAt)}`],
    ["Measured duration", run.elapsedSeconds === null ? "Unknown — not measured" : formatDuration(run.elapsedSeconds)],
    ["Stop reason", run.stoppedBecause ?? "None configured"],
    ["Last diagnostic", diagnosticCopy(run)],
    ["Source pause", sourcePauseCopy(run)],
  ];
}

function diagnosticCopy(run: RunProjection): string {
  const diagnostic = run.diagnostic;
  if (diagnostic === null) return "No diagnostic published";
  const parts = [
    diagnostic.type === null ? null : `type ${diagnostic.type}`,
    diagnostic.head,
    diagnostic.at === null ? null : `observation ${timestamp(diagnostic.at)}`,
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? "Diagnostic published without usable content" : parts.join(' · ');
}

function sourcePauseCopy(run: RunProjection): string {
  const pause = run.sourcePause;
  if (pause === null) return "No source pause configured";
  const parts = [
    pause.retryAfter === null ? null : `recovery after ${timestamp(pause.retryAfter)}`,
    pause.reasonCode === null ? null : `code ${pause.reasonCode}`,
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? "Pause configured without a usable deadline" : parts.join(' · ');
}

/* ------------------------------------------------------------------ */
/* Identité du flux — journal, objets, lecteur, job                     */
/* ------------------------------------------------------------------ */

export function fluxIdentityRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const flux = pipeline.flux ?? null;
  if (flux === null) {
    return [
      ["Flow", "Not measured — measurement not published by the service"],
      ['Journal', NOT_INSTRUMENTED],
      ["Configured objects", NOT_INSTRUMENTED],
      ["Reader path", NOT_INSTRUMENTED],
      ["Target", "Not measured"],
      ['Job', NOT_INSTRUMENTED],
    ];
  }
  return [
    ["Flow", [flux.label, flux.id].filter((part): part is string => part !== null).join(' · ') || "Unknown — not published"],
    ['Journal', journalCopy(flux)],
    ["Configured objects", flux.objects === null ? "Unknown — not published" : (flux.objects.length === 0 ? "No objects configured" : flux.objects.join(', '))],
    ["Reader path", flux.readerPath ?? "Unknown — not published"],
    ["Target", flux.target ?? "Unknown — not published"],
    ['Job', flux.job ?? "Unknown — not published"],
  ];
}

function journalCopy(flux: FluxIdentity): string {
  if (flux.journal === null && flux.journalLibrary === null) return "Unknown — not published";
  if (flux.journal === null) return flux.journalLibrary!;
  if (flux.journalLibrary === null) return flux.journal;
  return `${flux.journalLibrary}/${flux.journal}`;
}

/** Couverture d'une table par les objets déclarés du flux — L2. */
export function fluxObjectCoverage(flux: FluxIdentity | null | undefined, table: string): string {
  if (flux === undefined || flux === null) {
    return "Not measured — connection identity absent from the observation";
  }
  const objects = flux.objects;
  if (objects === null) {
    return "Not measured — connection content not published";
  }
  const needle = table.toUpperCase();
  const covered = objects.some((object) => {
    const candidate = object.toUpperCase();
    return candidate === needle || candidate.endsWith(`.${needle}`) || candidate.endsWith(`/${needle}`);
  });
  return covered ? "Configured in the connection" : "Outside the configured scope";
}

/* ------------------------------------------------------------------ */
/* Destination — checkpoints, comptes, run tag (L3)                     */
/* ------------------------------------------------------------------ */

export function destinationRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const destination = pipeline.destination ?? null;
  if (destination === null) {
    const reason = pipeline.destination === null
      ? destinationReasonLabel(pipeline.destinationReason)
      : "measurement not published by the service";
    return [
      ["Destination block", pipeline.destination === null ? `Not linked — ${reason}` : `Not measured — ${reason}`],
      ["Loader checkpoint", NOT_INSTRUMENTED],
      ["Application checkpoint", NOT_INSTRUMENTED],
      ["Source events", NOT_INSTRUMENTED],
      ["Raw rows", NOT_INSTRUMENTED],
      ["Canonical rows", NOT_INSTRUMENTED],
      ["Duplicates", NOT_INSTRUMENTED],
    ];
  }
  return [
    ["Destination observation", destination.observedAt === null ? "Unknown — not measured" : timestamp(destination.observedAt)],
    ["Provenance", destination.kind ?? "Unknown — not published"],
    ["Database", destination.database ?? "Unknown — not published"],
    ["Schema", destination.schema ?? "Unknown — not published"],
    ['Stage', destination.stage ?? "Unknown — not published"],
    ["Raw table", destination.rawTable ?? "Unknown — not published"],
    ["Canonical table", destination.canonicalTable ?? "Unknown — not published"],
    ['Run tag', destination.runTag ?? "Unknown — not published"],
    ["Loader checkpoint", destinationCheckpointCopy(destination.loadCheckpoint)],
    ["Application checkpoint", destinationCheckpointCopy(destination.applyCheckpoint)],
    ["Source events", countCopy(destination.sourceEvents)],
    ["Raw rows", countCopy(destination.rawRows)],
    ["Canonical rows", countCopy(destination.canonicalRows)],
    ["Duplicates", countCopy(destination.duplicates)],
  ];
}

function destinationCheckpointCopy(checkpoint: JournalCheckpoint | null): string {
  return checkpoint === null ? "Unknown — not measured" : position(checkpoint.receiver, checkpoint.sequence);
}

function countCopy(value: number | null): string {
  return value === null ? "Unknown — not measured" : sequences(value);
}

/** Registre L3 : toutes les lignes destination, regroupées par section. */
export function destinationDetailSections(pipeline: Pipeline): {
  readonly applyStamp: ApplyStamp;
  readonly rows: ReadonlyArray<readonly [string, string]>;
} {
  return { applyStamp: destinationApplyStamp(pipeline), rows: destinationRows(pipeline) };
}
