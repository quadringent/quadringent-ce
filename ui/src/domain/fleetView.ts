import { exact, sequences } from './format.ts';
import { siteIdentity, type SiteIdentity } from './siteIdentity.ts';
import type {
  CapabilityId,
  Coverage,
  EvidenceKind,
  Freshness,
  Fleet,
  FleetPlan,
  FleetPlanTable,
  FleetRuntime,
  FleetRuntimePhase,
  FleetRuntimeTablePhase,
  FleetPhase,
  FleetSafeAction,
  FleetTable,
  ReconciliationProof,
} from './controlPlane.ts';

export const UNMEASURED = "Not measured";

/**
 * Copy intended for the landing view.  The catalogue, execution state and
 * technical receipt remain separate concerns: the public view must not turn a
 * catalogue row count into a claim that rows reached Snowflake.
 */
export type PublicStateTone = 'neutral' | 'positive' | 'attention' | 'blocked';

export interface PublicStateCopy {
  readonly label: string;
  readonly tone: PublicStateTone;
}

/**
 * Jetons de confirmation opérateur, dérivés de l'identité déclarée du site
 * — la même convention que le control plane : « ACTION SITE ENVIRONNEMENT ».
 * Aucune valeur n'est figée dans l'interface.
 */
export function fleetActionConfirmations(
  site: SiteIdentity = siteIdentity(),
): Readonly<Record<CapabilityId, string | null>> {
  const declared = `${site.siteId.toUpperCase()} ${site.environment}`;
  return {
    refresh: null,
    prepare: `PREPARE ${declared}`,
    start: `START ${declared}`,
    pause: `PAUSE ${declared}`,
    resume: `RESUME ${declared}`,
  };
}

/**
 * Corps exact accepté par le serveur d'actions : {fleet_id, environment,
 * confirmation}. Aucune clé supplémentaire n'est jamais envoyée — le repli
 * historique « dataset » a été retiré du contrat.
 */
export function fleetActionRequest(
  site: SiteIdentity,
  action: CapabilityId,
): { readonly fleet_id: string; readonly environment: string; readonly confirmation: string | null } {
  return {
    fleet_id: site.fleetId,
    environment: site.runtimeEnvironment,
    confirmation: fleetActionConfirmations(site)[action],
  };
}

const phaseLabels: Readonly<Record<FleetPhase, string>> = {
  NOT_PREPARED: "To prepare",
  READY: "Ready",
  HISTORICAL: "History",
  CATCHING_UP: "Catching up",
  LIVE: "Real time",
  RECONCILING: "Reconciliation",
  CERTIFIED: "Verified",
  PAUSED: "Paused",
  BLOCKED: "Blocked",
};

const phaseScopes: Readonly<Record<FleetPhase, 'partial' | 'complete' | 'gap'>> = {
  NOT_PREPARED: 'partial',
  READY: 'partial',
  HISTORICAL: 'partial',
  CATCHING_UP: 'partial',
  LIVE: 'partial',
  RECONCILING: 'partial',
  CERTIFIED: 'complete',
  PAUSED: 'partial',
  BLOCKED: 'gap',
};

const nextActionCopy: Readonly<Record<FleetSafeAction, string>> = {
  PREPARE: "Prepare tables",
  ADMIT_HISTORICAL: "Start the historical copy",
  RECORD_HISTORY_PROGRESS: "Continue the historical copy",
  PROVE_CONTINUITY: "Verify journal continuity",
  CATCH_UP_TO_TAIL: "Catch up with the journal",
  RECORD_ACTUAL_COST: "Record the actual cost",
  OPEN_RECONCILIATION: "Open reconciliation",
  CERTIFY: "Verify the table",
  RESUME: "Resume",
  INSPECT_BLOCKED: "Inspect the blocker",
  NONE: "No safe action",
};

const utcFormat = new Intl.DateTimeFormat("en-US", { hourCycle: 'h23',
  day: '2-digit',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
});

const dayFormat = new Intl.DateTimeFormat("en-US", { hourCycle: 'h23',
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});

export function formatUtc(value: string): string {
  return `${utcFormat.format(new Date(value))} UTC`;
}

export function fleetObservationCopy(evidenceKind: EvidenceKind, observedAt: string): string {
  if (evidenceKind === 'live') return "Observation in progress";
  if (evidenceKind === 'simulation') return "Simulated observation";
  return `Historical observation · ${dayFormat.format(new Date(observedAt))}`;
}

export function phaseLabel(phase: FleetPhase): string {
  return phaseLabels[phase];
}

export function phaseScope(phase: FleetPhase): 'partial' | 'complete' | 'gap' {
  return phaseScopes[phase];
}

/** The short, non-technical state shown in the main table. */
export function publicPhaseLabel(phase: FleetPhase): string {
  switch (phase) {
    case 'NOT_PREPARED': return "To prepare";
    case 'READY': return "Ready";
    case 'HISTORICAL':
    case 'CATCHING_UP':
    case 'RECONCILING': return "In progress";
    case 'LIVE':
    case 'CERTIFIED': return "Available";
    case 'PAUSED': return "Paused";
    case 'BLOCKED': return "Blocked";
  }
}

export function runtimePhaseLabel(phase: FleetRuntimePhase | FleetRuntimeTablePhase): string {
  switch (phase) {
    case 'NOT_PREPARED': return "To prepare";
    case 'READY': return "Ready";
    case 'PREPARED': return "Ready to start";
    case 'HISTORICAL': return "Initial copy in progress";
    case 'CATCHING_UP': return "Catching up";
    case 'LIVE': return "Real time";
    case 'RECONCILING': return "Verification in progress";
    case 'CERTIFIED': return "Verified";
    case 'PAUSED': return "Paused";
    case 'BLOCKED': return "Blocked";
    case 'UNKNOWN': return "To confirm";
  }
}

/** Runtime status for the updates column. A plan-only row has not started. */
export function runtimeUpdatesCopy(phase: FleetRuntimePhase | FleetRuntimeTablePhase | null | undefined): string {
  switch (phase) {
    case 'HISTORICAL': return "To confirm";
    case 'CATCHING_UP':
    case 'LIVE':
    case 'RECONCILING':
    case 'CERTIFIED': return "Observed";
    case 'BLOCKED': return "Blocked";
    case 'UNKNOWN': return "To confirm";
    case 'NOT_PREPARED':
    case 'READY':
    case 'PREPARED':
    case 'PAUSED':
    case undefined:
    case null: return "Not started yet";
  }
}

function rowProgressCopy(copiedRows: number | null, totalRows: number): string {
  if (copiedRows === null) return "In progress";
  return `${sequences(copiedRows)} / ${sequences(totalRows)} rows`;
}

/** Source/catalogue volume, explicitly not a destination volume. */
export function fleetPlanDataCopy(table: FleetPlanTable): string {
  return `${sequences(table.rowCount)} source rows`;
}

export function fleetTableDataCopy(table: FleetTable): string {
  return table.totalRows === null ? "Volume not measured" : `${sequences(table.totalRows)} source rows`;
}

export function fleetPlanInitialCopy(
  table: FleetPlanTable,
  runtimePhase: FleetRuntimePhase | FleetRuntimeTablePhase | null | undefined = null,
): string {
  // Admission in the catalogue is only a planning decision. Without an
  // observed runtime phase it must not look like preparation or execution has
  // already happened.
  if (runtimePhase === null || runtimePhase === undefined) return "To prepare";
  if (runtimePhase === 'BLOCKED') return "Blocked";
  if (runtimePhase === 'UNKNOWN') return "To confirm";
  // Le domaine n'admet CATCHING_UP qu'une fois la copie mesurée complète
  // (copied == total) : dès cette phase, la copie initiale est terminée.
  if (
    runtimePhase === 'CATCHING_UP'
    || runtimePhase === 'LIVE'
    || runtimePhase === 'RECONCILING'
    || runtimePhase === 'CERTIFIED'
  ) return "Completed";
  if (runtimePhase === 'HISTORICAL') return rowProgressCopy(table.copiedRows, table.rowCount);
  if (runtimePhase === 'PAUSED') return "Paused";
  if (runtimePhase === 'READY' || runtimePhase === 'PREPARED') return "Ready to start";
  if (runtimePhase === 'NOT_PREPARED') return "To prepare";
  return "To confirm";
}

export function fleetPlanUpdatesCopy(
  _table: FleetPlanTable,
  runtimePhase: FleetRuntimePhase | FleetRuntimeTablePhase | null | undefined = null,
): string {
  return runtimeUpdatesCopy(runtimePhase);
}

export function fleetPlanTableStateCopy(
  _table: FleetPlanTable,
  runtimePhase: FleetRuntimePhase | FleetRuntimeTablePhase | null | undefined = null,
): string {
  if (runtimePhase !== null && runtimePhase !== undefined) return runtimePhaseLabel(runtimePhase);
  return "To prepare";
}

export function fleetInitialCopy(table: FleetTable, observationCurrent = true): string {
  if (!observationCurrent) return "To confirm";
  switch (table.phase) {
    case 'NOT_PREPARED': return "Not started yet";
    case 'READY': return "Ready to start";
    case 'HISTORICAL':
    case 'CATCHING_UP':
    case 'RECONCILING':
      return table.totalRows !== null ? rowProgressCopy(table.copiedRows, table.totalRows) : "In progress";
    case 'LIVE':
    case 'CERTIFIED':
      // A runtime phase alone does not prove that the initial copy reached
      // its source total. In particular, LIVE may be emitted before the
      // copied-row counters are available.
      if (table.copiedRows === null || table.totalRows === null) return "To confirm";
      return table.copiedRows === table.totalRows
        ? "Completed"
        : rowProgressCopy(table.copiedRows, table.totalRows);
    case 'PAUSED': return "Paused";
    case 'BLOCKED': return "Blocked";
  }
}

export function fleetUpdatesCopy(table: FleetTable, observationCurrent = true): string {
  if (!observationCurrent) return "To confirm";
  if (table.phase === 'NOT_PREPARED' || table.phase === 'READY') return "Not started yet";
  if (table.phase === 'BLOCKED') return "Blocked";
  if (table.phase === 'PAUSED' && table.currentCheckpoint === null) return "Paused";
  if (table.phase === 'HISTORICAL') return "To confirm";
  if (table.currentCheckpoint !== null || table.journalTail !== null || table.continuityProven === true) {
    return "Observed";
  }
  return "To confirm";
}

export function fleetTableStateCopy(table: FleetTable): string {
  return publicPhaseLabel(table.phase);
}

export function fleetStateCopy(
  fleet: Fleet,
  evidenceKind: EvidenceKind = 'live',
  cached = false,
  quality: { readonly coverage?: Coverage; readonly freshness?: Freshness } = {},
): PublicStateCopy {
  if (
    cached
    || evidenceKind !== 'live'
    || (quality.coverage !== undefined && quality.coverage !== 'complete')
    || (quality.freshness !== undefined && quality.freshness !== 'fresh')
  ) return { label: "To confirm", tone: 'attention' };
  if (fleet.tables.some((table) => table.phase === 'BLOCKED') || fleet.summary.overBudget) {
    return { label: "Blocked", tone: 'blocked' };
  }
  if (fleet.summary.tableCount > 0 && fleet.summary.certifiedCount === fleet.summary.tableCount) {
    return { label: "Available", tone: 'positive' };
  }
  if (fleet.summary.runningCount > 0 || fleet.summary.admittedCount > 0) {
    return { label: "In progress", tone: 'attention' };
  }
  if (fleet.summary.nextAction === 'PREPARE') return { label: "To prepare", tone: 'attention' };
  return { label: "To confirm", tone: 'attention' };
}

export function fleetPlanStateCopy(
  plan: FleetPlan,
  runtime: FleetRuntime | null,
  cached = false,
): PublicStateCopy {
  if (cached) return { label: "To confirm", tone: 'attention' };
  // Le runtime est projeté en direct par l'exécuteur : sa phase est une
  // mesure actuelle indépendante de l'âge du relevé catalogue, qui ne
  // qualifie que les données déclaratives (volumes, découpage).
  if (runtime) {
    switch (runtime.phase) {
      case 'BLOCKED': return { label: "Blocked", tone: 'blocked' };
      case 'UNKNOWN': return { label: "To confirm", tone: 'attention' };
      case 'NOT_PREPARED': return { label: "To prepare", tone: 'attention' };
      case 'PREPARED': return { label: "Ready to start", tone: 'attention' };
      case 'HISTORICAL':
      case 'CATCHING_UP':
      case 'RECONCILING': return { label: "In progress", tone: 'attention' };
      case 'LIVE':
      case 'CERTIFIED': return { label: "Available", tone: 'positive' };
      case 'PAUSED': return { label: "Paused", tone: 'neutral' };
    }
  }
  if (plan.freshness !== 'fresh') return { label: "To confirm", tone: 'attention' };
  return { label: "Not established", tone: 'attention' };
}

export function evidenceKindCopy(kind: EvidenceKind): string {
  switch (kind) {
    case 'live': return "Live";
    case 'historical': return "Historical observation";
    case 'simulation': return 'Simulation';
  }
}

export function certifiedCopy(fleet: Fleet): string {
  return `${sequences(fleet.summary.certifiedCount)} / ${sequences(fleet.summary.tableCount)}`;
}

export function fleetVolumeCopy(fleet: Fleet): string {
  return fleet.summary.knownTotalRows === null
    ? "Source volume not measured"
    : `${sequences(fleet.summary.knownTotalRows)} identified source rows`;
}

export function historyCopy(table: FleetTable): string {
  if (table.copiedRows === null && table.totalRows === null) return UNMEASURED;
  if (table.copiedRows !== null && table.totalRows !== null) {
    return `${sequences(table.copiedRows)} / ${sequences(table.totalRows)}`;
  }
  if (table.copiedRows !== null) return `${sequences(table.copiedRows)} rows`;
  return `${sequences(table.totalRows as number)} in total`;
}

export function journalCopy(table: FleetTable): string {
  const checkpoint = table.currentCheckpoint ?? table.journalTail ?? table.startCheckpoint;
  if (!checkpoint) return UNMEASURED;
  return `${checkpoint.receiver} · ${sequences(checkpoint.sequence)}`;
}

export function reconciliationCopy(table: FleetTable): string {
  const proof = table.reconciliationProof;
  if (!proof) return UNMEASURED;
  if (proof.missing === 0 && proof.extra === 0 && proof.duplicates === 0 && proof.sourceHash === proof.targetHash) {
    return "Compliant";
  }
  return `Differences ${sequences(proof.missing + proof.extra + proof.duplicates)}`;
}

export function performanceCopy(proof: ReconciliationProof | null): string {
  if (!proof) return UNMEASURED;
  return `${exact(proof.latencySeconds)} s · ${exact(proof.throughputRowsPerSecond)} lig/s`;
}

export function costCopy(table: FleetTable): string {
  if (table.actualCredits !== null) return `${exact(table.actualCredits)} credit`;
  if (table.reservedCredits !== null && table.reservedCredits !== 0) {
    return `${exact(table.reservedCredits)} reserved`;
  }
  if (table.estimatedCredits !== null) return `${exact(table.estimatedCredits)} estimated`;
  return UNMEASURED;
}

export function resultCopy(table: FleetTable): string {
  const parts = [reconciliationCopy(table)];
  const performance = performanceCopy(table.reconciliationProof);
  const cost = costCopy(table);
  if (performance !== UNMEASURED) parts.push(performance);
  if (cost !== UNMEASURED) parts.push(cost);
  return parts.join(' · ');
}

export function nextActionLabel(fleet: Fleet): string {
  if (fleet.summary.nextAction === 'PREPARE') {
    return `Prepare the ${sequences(fleet.summary.tableCount)} tables`;
  }
  return nextActionCopy[fleet.summary.nextAction];
}

export function capabilityReasonCopy(reason: string | null, action: CapabilityId): string | null {
  if (reason === null) return null;
  if (reason === 'not_live' || reason === 'stale_proof') {
    return action === 'refresh'
      ? "The displayed observation is old. An active source must produce a new observation."
      : "The displayed observation is old. Controls resume after the active source reconnects.";
  }
  if (reason === 'refresh_unwired') {
    return "Fleet refresh is not yet connected to the service.";
  }
  if (reason === 'operator_access_unavailable') {
    return `Operator access ${siteIdentity().environment} required for this command is unavailable.`;
  }
  return action === 'refresh'
    ? "Refresh is unavailable for this observation."
    : "Controls are unavailable for this observation.";
}

export function nextActionDetail(fleet: Fleet): string {
  const table = fleet.summary.nextTable;
  const scope = table ? ` for ${table}` : '';
  switch (fleet.summary.nextAction) {
    case 'PREPARE':
      return `Record the starting point for the ${sequences(fleet.summary.tableCount)} tables before any historical copy.`;
    case 'ADMIT_HISTORICAL':
      return `The prerequisites are met${scope}. The historical copy can start within the planned capacity limit.`;
    case 'RECORD_HISTORY_PROGRESS':
      return `The historical copy is in progress${scope}. Refresh row counts before the next stage.`;
    case 'PROVE_CONTINUITY':
      return `The historical copy is complete${scope}. Verify that no IBM i update was lost during the transition.`;
    case 'CATCH_UP_TO_TAIL':
      return `Apply updates made during the historical copy${scope}, until processing reaches real time.`;
    case 'RECORD_ACTUAL_COST':
      return `Actual usage must be recorded${scope} before continuing.`;
    case 'OPEN_RECONCILIATION':
      return `Compare row counts, fingerprints, missing rows, extra rows and duplicates${scope}.`;
    case 'CERTIFY':
      return `All required evidence is available${scope}. The table can be verified.`;
    case 'RESUME':
      return `The paused table${scope} can resume from its last proven point.`;
    case 'INSPECT_BLOCKED':
      return `Evidence prevents continuation${scope}. Open the details before resuming.`;
    case 'NONE':
      return `The ${sequences(fleet.summary.tableCount)} tables are verified; no further action is required.`;
  }
}

/** Short action copy for the landing view; detailed operator terminology stays
 * in the technical drawer. */
export function publicNextActionLabel(fleet: Fleet): string {
  switch (fleet.summary.nextAction) {
    case 'PREPARE': return `Prepare the ${sequences(fleet.summary.tableCount)} tables`;
    case 'ADMIT_HISTORICAL': return "Start the initial copy";
    case 'RESUME': return "Resume";
    case 'INSPECT_BLOCKED': return "See the blocker";
    case 'NONE': return "Refresh state";
    default: return "Refresh state";
  }
}

export function publicNextActionDetail(fleet: Fleet): string {
  switch (fleet.summary.nextAction) {
    case 'PREPARE': return "Record the starting point before starting the initial copy.";
    case 'ADMIT_HISTORICAL': return `The ${sequences(fleet.summary.tableCount)} tables are ready. The initial copy can start.`;
    case 'RECORD_HISTORY_PROGRESS': return "The initial copy is in progress. Read the state again before continuing.";
    case 'PROVE_CONTINUITY': return "Read updates again to confirm that they have all been applied.";
    case 'CATCH_UP_TO_TAIL': return "Catch up with updates made during the initial copy.";
    case 'RECORD_ACTUAL_COST': return "Actual cost must be observed before continuing.";
    case 'OPEN_RECONCILIATION': return "Compare received and expected data before continuing.";
    case 'CERTIFY': return "All measurements are available. Open the details to continue.";
    case 'RESUME': return "Recovery can start from the last recorded point.";
    case 'INSPECT_BLOCKED': return "A stage prevents continuation. Open the details before resuming.";
    case 'NONE': return "No next action is required in the current observation.";
  }
}
