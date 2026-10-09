/**
 * Couche opérateur — la seule couche autorisée à produire du texte d'écran.
 *
 * Elle traduit la projection du service en quatre réponses, dans cet ordre :
 *
 *   1. quel état          → `health` + `stateLabel` (une couleur, un mot)
 *   2. quel fait chiffré  → `facts` (trois au maximum, mesurés ou tus)
 *   3. quoi faire         → `guidance` + `actions`
 *   4. sur quelle preuve  → `evidence` (relégué en profondeur)
 *
 * Trois règles tiennent ce module :
 *
 * - Ce qui n'est pas mesuré n'est pas affiché. Une valeur absente vaut `null`
 *   et se rend « Non mesuré » ; elle ne devient jamais 0, ni une estimation,
 *   ni un libellé technique de repli.
 * - Ce qui est interne n'est pas traduit, il est tu. Les compteurs de boucle
 *   du lecteur (sondages, balayages à vide, rotations) ne deviennent pas
 *   « lectures à vide » : ils ne remontent pas jusqu'ici.
 * - Une action n'est offerte que si le service la déclare disponible. Une
 *   capacité indisponible ne produit ni bouton, ni injonction : elle produit
 *   au plus une consigne qui dit où l'acte se fait réellement.
 */

import type { EvidenceKind, FleetCapability, Pipeline, PipelineStatus } from './controlPlane.ts';
import type { ActionId } from '../data/controlPlaneClient.ts';
import { age, sequences } from './format.ts';
import type { PipelineActionReceipt } from '../data/controlPlaneClient.ts';
import type { ControlActionKind, ControlLevel } from './controlsPanel.ts';

/** Santé d'une liaison — ce que la couleur porte, et rien d'autre. */
export type Health = 'ok' | 'attention' | 'stopped' | 'unknown';

/** Un fait chiffré. `value === null` signifie « mesure absente », jamais zéro. */
export interface Fact {
  readonly label: string;
  readonly value: string | null;
  /** Précision courte, affichée sous la valeur. Omise si elle répète le label. */
  readonly note?: string;
}

/** Une action réellement exécutable : le service l'a déclarée disponible. */
export interface ActionOffer {
  readonly id: ActionId;
  readonly label: string;
  /** Ce que l'action fait, en une phrase, pour la confirmation. */
  readonly effect: string;
}

export interface LiaisonView {
  readonly id: string;
  /** Titre affiché. Reste l'identifiant tant que le service n'en publie pas d'autre. */
  readonly name: string;
  /** Où les données arrivent, en clair. `null` si le service ne le dit pas. */
  readonly destination: string | null;
  readonly health: Health;
  /** L'état en deux mots au plus. */
  readonly stateLabel: string;
  /** La phrase principale : ce qui se passe, sans jargon. */
  readonly headline: string;
  /** Ce que l'opérateur doit faire. `null` s'il n'y a rien à faire. */
  readonly guidance: string | null;
  readonly facts: readonly Fact[];
  readonly actions: readonly ActionOffer[];
  /** Âge du relevé, en clair. */
  readonly freshness: string;
  /** Vrai quand le relevé est trop vieux pour qu'on affirme quoi que ce soit. */
  readonly outdated: boolean;
  /**
   * Avertissement sur la nature de ce qui est affiché, ou `null` quand il
   * s'agit d'une lecture réelle et courante.
   *
   * Une démonstration et un rejeu d'historique se rendent à l'écran comme une
   * mesure : rien ne les distingue d'un relevé du jour. Un exploitant qui
   * prend une décision sur un écran de simulation ne s'en rend compte qu'après.
   */
  readonly caveat: string | null;
}

const MEASURE_ABSENT = "Not measured";

/** Rend une valeur de fait. Le seul endroit où l'absence prend un mot. */
export function factValue(fact: Fact): string {
  return fact.value ?? MEASURE_ABSENT;
}

/* ------------------------------------------------------------------ */
/* État                                                                */
/* ------------------------------------------------------------------ */

interface StateCopy {
  readonly health: Health;
  readonly label: string;
}

/**
 * L'état du service devient un état d'opérateur.
 *
 * `awaiting_resume` est délibérément rendu « Lecture arrêtée » et non « Prête
 * à reprendre » : du point de vue de l'exploitant, le fait qui compte est que
 * plus rien n'est lu. Que la cause soit levée appartient à la phrase, pas à
 * l'état — sans quoi l'écran a l'air d'aller bien alors qu'il ne lit plus.
 */
export function stateCopy(status: PipelineStatus): StateCopy {
  switch (status) {
    case 'healthy':
      return { health: 'ok', label: "Up to date" };
    case 'recovering':
      return { health: 'attention', label: "Catching up" };
    case 'degraded':
      return { health: 'attention', label: "Needs attention" };
    case 'incident':
      return { health: 'stopped', label: "Interrupted" };
    case 'planned_stop':
      return { health: 'stopped', label: "Intentional stop" };
    case 'awaiting_resume':
      return { health: 'stopped', label: "Reader stopped" };
    case 'unknown':
      return { health: 'unknown', label: "Unknown state" };
  }
}

/* ------------------------------------------------------------------ */
/* Phrase principale                                                   */
/* ------------------------------------------------------------------ */

/**
 * Durée au registre de la conversation : « 2 jours », pas « 44 h 32 ».
 *
 * L'arrondi est délibéré et n'entame pas la règle de vérité : il porte sur un
 * écart de temps, jamais sur une mesure publiée par le service. La durée
 * exacte reste lisible à l'endroit qui la prouve — l'onglet Mesures.
 */
export function approximateDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  if (s < 60) return "less than a minute";
  const minutes = Math.round(s / 60);
  if (minutes < 60) return minutes === 1 ? '1 minute' : `${minutes} minutes`;
  const hours = Math.round(s / 3600);
  if (hours < 24) return hours === 1 ? "1 hour" : `${hours} hours`;
  const days = Math.round(s / 86400);
  if (days < 7) return days === 1 ? "1 day" : `${days} days`;
  const weeks = Math.round(s / 604800);
  return weeks === 1 ? "1 week" : `${weeks} weeks`;
}

/** Depuis combien de temps la lecture est interrompue, en clair. */
function stoppedSince(pipeline: Pipeline, now: Date): string | null {
  const at = pipeline.incident?.declaredAt ?? pipeline.run?.diagnostic?.at ?? null;
  if (!at) return null;
  const seconds = (now.getTime() - new Date(at).getTime()) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return approximateDuration(seconds);
}

/**
 * La phrase que l'opérateur lit en premier.
 *
 * Elle décrit une situation, jamais un mécanisme : ni position de lecture, ni
 * nom de tâche, ni état de protocole. Quand la cause d'un arrêt est levée, la
 * phrase le dit — c'est ce qui distingue une panne en cours d'un arrêt qui
 * n'attend plus que d'être relancé.
 */
export function headlineFor(pipeline: Pipeline, now: Date): string {
  const { status } = pipeline;
  const since = stoppedSince(pipeline, now);

  if (status === 'awaiting_resume') {
    const base = since ? `Stopped for ${since}.` : "The reader is stopped.";
    return pipeline.incident?.causeResolved
      ? `${base} The source connection is working again.`
      : base;
  }
  if (status === 'incident') {
    return since ? `Interrupted for ${since}.` : "The reader is interrupted.";
  }
  if (status === 'planned_stop') {
    return since ? `Intentionally stopped for ${since}.` : "Intentionally stopped.";
  }
  if (status === 'recovering') return "The reader has resumed and is catching up.";
  if (status === 'degraded') return "The copy is progressing, but a check is missing.";
  if (status === 'unknown') return "The latest observation is inconclusive.";
  return "The copy is keeping up with the source.";
}

/* ------------------------------------------------------------------ */
/* Consigne                                                            */
/* ------------------------------------------------------------------ */

/**
 * Ce que l'opérateur doit faire, quand il ne peut pas le faire ici.
 *
 * Cette fonction n'est appelée que si aucune action n'est disponible. Elle ne
 * donne jamais un ordre en l'air : soit elle dit où l'acte se fait, soit elle
 * se tait. Une consigne sans destinataire vaut moins que pas de consigne.
 */
export function guidanceFor(pipeline: Pipeline, offered: readonly ActionOffer[]): string | null {
  if (offered.length > 0) return null;
  const { status } = pipeline;

  if (status === 'awaiting_resume' || status === 'incident') {
    // Le service dit pourquoi la reprise n'est pas offerte. La distinction
    // porte : « ce déploiement ne sait pas relancer » appelle l'exploitation,
    // « les vérifications ne sont pas réunies » appelle l'attente.
    const reason = pipeline.fleetRuntime?.capabilities?.resume?.reason ?? null;
    if (reason === 'resume_not_ready') {
      return "Recovery is not safe yet: some recovery checks are still missing. This screen will show when they are complete.";
    }
    if (reason === 'not_pausable_in_phase') {
      // Le produit sait relancer, mais pas dans l'état où se trouve la copie.
      // Renvoyer vers l'exploitation serait faux : il n'y a rien à demander.
      return "The copy is not in a state where recovery applies. Refresh to see whether this changes.";
    }
    if (!pipeline.incident?.causeResolved) {
      return "The cause is unresolved. Ask your operations team to address it before restarting.";
    }
    return "Restart cannot be controlled from Quadringent. Ask your operations team.";
  }

  if (status === 'unknown') {
    return "No usable observation. Report this if the screen remains in this state.";
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Actions                                                             */
/* ------------------------------------------------------------------ */

const ACTION_COPY: Readonly<Record<ActionId, { label: string; effect: string }>> = {
  refresh: { label: "Refresh", effect: "Reads the state from the service again." },
  prepare: { label: "Prepare tables", effect: "Creates the destination locations." },
  start: { label: "Start the copy", effect: "Starts the first copy, then continuous tracking." },
  pause: { label: "Pause", effect: "Stops the reader. The position is retained." },
  resume: { label: "Restart", effect: "Resumes the reader where it stopped." },
};

const PILOT_ACTIONS: readonly ActionId[] = ['prepare', 'start', 'pause', 'resume'];

/**
 * Les actions offertes — strictement celles que le service déclare disponibles.
 *
 * `refresh` est exclu : relire l'état n'est pas une décision d'exploitation, il
 * a sa place dans l'en-tête de page, pas dans le bloc d'action d'une liaison.
 */
export function actionsFor(
  capabilities: Readonly<Record<string, FleetCapability>> | null | undefined,
): readonly ActionOffer[] {
  if (!capabilities) return [];
  return PILOT_ACTIONS.flatMap((id) => {
    const capability = capabilities[id];
    if (!capability || capability.state !== 'available') return [];
    return [{ id, label: ACTION_COPY[id].label, effect: ACTION_COPY[id].effect }];
  });
}

/* ------------------------------------------------------------------ */
/* Faits chiffrés                                                      */
/* ------------------------------------------------------------------ */

/**
 * Les trois faits qui répondent à « c'est à jour ? ».
 *
 * Ne remontent ici que des grandeurs qu'un exploitant peut vérifier lui-même
 * dans Snowflake : des lignes, des tables, des doublons. Les compteurs propres
 * au lecteur restent dans l'onglet Mesures.
 *
 * Aucune valeur n'est reconstituée : si le service ne publie pas la mesure,
 * le fait vaut `null` et l'écran écrit « Non mesuré ».
 */
export function factsFor(pipeline: Pipeline): readonly Fact[] {
  const destination = pipeline.destination;
  const tables = pipeline.fleetRuntime?.tableStates ?? [];
  const copied = tables.filter((table) => table.phase === 'CERTIFIED').length;

  const rows = destination?.canonicalRows ?? destination?.rawRows ?? null;
  const duplicates = destination?.duplicates ?? null;

  return [
    {
      label: "Rows in Snowflake",
      value: rows === null ? null : sequences(rows),
    },
    {
      label: "Tables copied",
      value: tables.length === 0 ? null : `${copied} out of ${tables.length}`,
    },
    {
      label: "Duplicates",
      value: duplicates === null ? null : duplicates === 0 ? "None" : sequences(duplicates),
    },
  ];
}

/* ------------------------------------------------------------------ */
/* Assemblage                                                          */
/* ------------------------------------------------------------------ */

/** Destination en clair, sans identifiant interne quand il n'apporte rien. */
function destinationLabel(pipeline: Pipeline): string | null {
  const destination = pipeline.destination;
  if (!destination?.database || !destination.schema) return null;
  return `${destination.database}.${destination.schema}`;
}

/**
 * Le relevé le plus récent qui décrit vraiment la liaison.
 *
 * Les étapes ont chacune leur âge ; retenir le plus récent évite d'annoncer
 * « figé depuis deux jours » alors que la destination a été relue à l'instant.
 */
function latestObservation(pipeline: Pipeline): string | null {
  const candidates = [
    pipeline.observedAt,
    pipeline.destination?.observedAt ?? null,
    ...pipeline.stages.map((stage) => stage.observedAt),
  ].filter((value): value is string => typeof value === 'string' && value.length > 0);
  if (candidates.length === 0) return null;
  return candidates.reduce((latest, value) => (Date.parse(value) > Date.parse(latest) ? value : latest));
}

/**
 * Ce qu'il faut dire avant que l'écran ne soit pris pour argent comptant.
 *
 * L'ordre compte : une donnée qui n'est pas réelle prime sur une donnée
 * simplement vieille — savoir qu'on regarde une démonstration change tout,
 * savoir qu'elle date ne change que la confiance qu'on lui accorde.
 */
export function caveatFor(kind: EvidenceKind, outdated: boolean): string | null {
  if (kind === 'simulation') return "Demonstration — these numbers do not come from your system.";
  if (kind === 'historical') return "Archived observation — this is not the current state.";
  if (outdated) return "This observation is no longer recent: the state may have changed since.";
  return null;
}

export function liaisonView(pipeline: Pipeline, now: Date): LiaisonView {
  const { health, label } = stateCopy(pipeline.status);
  const actions = actionsFor(pipeline.fleetRuntime?.capabilities);
  const observedAt = latestObservation(pipeline);
  const observed = observedAt ? age(observedAt, now) : null;

  return {
    id: pipeline.id,
    name: pipeline.id,
    destination: destinationLabel(pipeline),
    health,
    stateLabel: label,
    headline: headlineFor(pipeline, now),
    guidance: guidanceFor(pipeline, actions),
    facts: factsFor(pipeline),
    actions,
    freshness: observed ? `Observation ${observed.label}` : "No observation",
    outdated: observed === null || observed.stale,
    caveat: caveatFor(pipeline.quality.evidenceKind, observed === null || observed.stale),
  };
}

/**
 * Verdict d'ensemble — ce que la page d'accueil annonce en un mot.
 *
 * Le pire état l'emporte : une liaison arrêtée parmi dix ne se noie pas dans
 * une moyenne rassurante.
 */
const SEVERITY: Readonly<Record<Health, number>> = {
  ok: 0,
  attention: 1,
  unknown: 2,
  stopped: 3,
};

export function worstHealth(views: readonly LiaisonView[]): Health {
  return views.reduce<Health>(
    (worst, view) => (SEVERITY[view.health] > SEVERITY[worst] ? view.health : worst),
    'ok',
  );
}

/** Les liaisons qui demandent une attention, d'abord ; l'ordre reste stable. */
export function byUrgency(views: readonly LiaisonView[]): readonly LiaisonView[] {
  return [...views].sort((a, b) => SEVERITY[b.health] - SEVERITY[a.health]);
}

/** Résumé d'accueil : un mot, éventuellement un compte. */
export function boardSummary(views: readonly LiaisonView[]): string {
  if (views.length === 0) return "No connections";
  const attention = views.filter((view) => view.health !== 'ok').length;
  if (attention === 0) return views.length === 1 ? "Your connection is up to date" : "All your connections are up to date";
  if (attention === 1) return '1 connection needs your attention';
  return `${attention} connections need your attention`;
}

export const OPERATOR_COPY = { MEASURE_ABSENT } as const;

/* ------------------------------------------------------------------ */
/* Étapes                                                              */
/* ------------------------------------------------------------------ */

/**
 * Les trois étapes que l'opérateur reconnaît.
 *
 * Le service en publie cinq (source, capture, raw, load, destination), mais
 * « raw » et « load » sont deux moments d'un même transfert : les distinguer
 * à l'écran oblige l'opérateur à connaître le découpage interne pour lire son
 * état. On garde donc le fait — les données sont-elles arrivées — et on laisse
 * le découpage à l'onglet Mesures.
 */
export type StepId = 'connexion' | 'lecture' | 'arrivee';

export interface Step {
  readonly id: StepId;
  readonly label: string;
  readonly health: Health;
  /** Ce qui est constaté, en langage courant. */
  readonly detail: string;
  /** Âge du constat. `null` si l'étape n'a jamais été observée. */
  readonly observed: string | null;
}

const STEP_SOURCES: Readonly<Record<StepId, { label: string; stages: readonly string[] }>> = {
  connexion: { label: "AS400 connection", stages: ['source'] },
  lecture: { label: "Reading data", stages: ['capture'] },
  arrivee: { label: "Arrival in Snowflake", stages: ['raw', 'load', 'destination'] },
};

function stepHealth(status: string): Health {
  if (status === 'healthy') return 'ok';
  if (status === 'degraded') return 'attention';
  if (status === 'incident' || status === 'planned_stop' || status === 'awaiting_resume') return 'stopped';
  return 'unknown';
}

/**
 * Réduit les étapes publiées à trois. Pour un regroupement, l'état retenu est
 * le moins bon : une arrivée « saine » qui masque un transfert en défaut
 * donnerait un écran faussement rassurant.
 */
export function stepsFor(pipeline: Pipeline, now: Date): readonly Step[] {
  return (Object.keys(STEP_SOURCES) as StepId[]).flatMap((id) => {
    const { label, stages } = STEP_SOURCES[id];
    const matching = pipeline.stages.filter((stage) => stages.includes(stage.id));
    if (matching.length === 0) return [];

    const worst = matching.reduce((acc, stage) =>
      SEVERITY[stepHealth(stage.status)] > SEVERITY[stepHealth(acc.status)] ? stage : acc,
    );
    const observedAt = matching
      .map((stage) => stage.observedAt)
      .filter((value): value is string => typeof value === 'string' && value.length > 0)
      .reduce<string | null>(
        (latest, value) => (latest === null || Date.parse(value) > Date.parse(latest) ? value : latest),
        null,
      );

    const health = stepHealth(worst.status);
    return [{
      id,
      label,
      health,
      detail: stepDetail(id, health),
      observed: observedAt === null ? null : age(observedAt, now).label,
    }];
  });
}

/**
 * Ce qu'une étape constate, dit par le produit et non par le service.
 *
 * Les libellés publiés par le service décrivent son propre découpage — « Raw
 * publié », « Prête à reprendre » — et se contredisent d'une étape à l'autre :
 * une étape « prête à reprendre » sous une liaison « arrêtée » laisse
 * l'opérateur arbitrer entre deux vocabulaires. Le texte est donc dérivé de
 * l'état constaté, qui lui est une donnée, et de l'étape concernée.
 */
function stepDetail(id: StepId, health: Health): string {
  if (health === 'unknown') return "No usable observation.";
  if (id === 'connexion') {
    return health === 'ok'
      ? "The AS400 responds and the account is accepted."
      : health === 'attention'
        ? "The connection responds, but a check is missing."
        : "The AS400 is not responding, or rejects the account.";
  }
  if (id === 'lecture') {
    return health === 'ok'
      ? "Changes are read continuously."
      : health === 'attention'
        ? "The reader is progressing, but a check is missing."
        : "Nothing is being read right now.";
  }
  return health === 'ok'
    ? "Data is being written to Snowflake."
    : health === 'attention'
      ? "Writes are progressing, but a check is missing."
      : "Nothing is arriving in Snowflake.";
}

/** Une fenêtre de métering mesure un warehouse ; elle n'est pas une facture de liaison. */
export function snowflakeCostFor(pipeline: Pipeline, now = new Date()) {
  const observation = pipeline.observability;
  const check = observation?.checks.find((item) => item.id === 'snowflake_credits');
  const window = check && /^metering_window_(\d{10})_(\d{10})_([1-9]\d{0,3})$/.exec(check.reason);
  const start = window ? Number(window[1]) : 0;
  const end = window ? Number(window[2]) : 0;
  const delay = (now.getTime() / 1000 - end) / 3600;
  const measured = check !== undefined && check.status !== 'unobserved'
    && check.unit === 'warehousecredits/delayed24h' && typeof check.observed === 'number'
    && Number.isFinite(check.observed) && check.observed >= 0 && window != null
    && end - start === 86400 && delay >= 6 && Number(window[3]) <= 1000;
  const kind = observation?.quality.evidenceKind;
  const observedAt = observation?.observedAt ? Date.parse(observation.observedAt) : NaN;
  const fresh = observation?.quality.freshness === 'fresh' && now.getTime() - observedAt <= 300_000
    && now.getTime() >= observedAt;
  const current = fresh && kind === 'live' && pipeline.quality?.evidenceKind === 'live';
  const costs = pipeline.costs;
  let amount = costs?.pricePerCredit == null ? "Price not configured" : "Not measured";
  if (measured && current && costs?.amount != null && costs.currency !== null) {
    amount = new Intl.NumberFormat("en-US", { style: 'currency', currency: costs.currency,
      minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(Number(costs.amount));
  }
  const caveat = kind === 'simulation' || pipeline.quality?.evidenceKind === 'simulation' ? "Simulated data"
    : kind === 'historical' || pipeline.quality?.evidenceKind === 'historical' ? "Historical observation" : observation && !fresh ? "Old observation or uncertain date" : null;
  return {
    creditsLabel: "Measured Snowflake credits", amountLabel: "Cost calculated at the configured rate",
    credits: measured ? new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 }).format(check!.observed as number) : null,
    amount,
    window: measured ? `24 h, ended ${new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(delay)} h · ${new Date(start * 1000).toISOString()} → ${new Date(end * 1000).toISOString()}` : "Window not measured",
    scope: costs ? `Entire warehouse ${costs.warehouse} · do not add across connections` : "Warehouse measurement · scope not published",
    caveat,
    explanation: "Amount calculated from measured credits and the configured contractual rate; this is not a bill.",
    absent: "Not measured",
  };
}

export function unmeasuredCostsFor(pipeline: Pipeline): readonly string[] {
  const missing: string[] = [];
  const costs = snowflakeCostFor(pipeline);
  if (costs.credits === null) missing.push("Consumed Snowflake credits have not been measured yet.");
  if (costs.amount === "Price not configured") missing.push("No rate configured: credits are shown without currency conversion.");
  if (!pipeline.infrastructureCosts?.storage || !pipeline.infrastructureCosts?.cluster) missing.push("S3 and cluster costs remain unmeasured when their observations are missing.");
  missing.push("S3 requests, AWS network transfers and other services are not priced.");
  return missing;
}
export const connectionNetworkFailure = "The service did not respond. Saving the connection is unconfirmed. Check saved connections before trying again.";

export function actionResultFor(pipelineId: string, receipt: PipelineActionReceipt): string {
  const result = receipt.stages.intent.state === 'rejected' && receipt.stages.execution.state === 'not_started'
    ? "Action rejected before execution."
    : receipt.state === 'succeeded' && receipt.stages.observedEffect.state === 'succeeded'
      ? "Action executed, effect confirmed by the service."
      : "The action's effect is unconfirmed. Refresh the state before trying again.";
  return `${pipelineId} : ${result} Receipt: ${receipt.id}.`;
}

export function actionFailureFor(pipelineId: string): string {
  return `${pipelineId}: the service did not confirm the action. Refresh the state before trying again.`;
}

/** Fenêtres et devises restent séparées : aucun total entre mesures incomparables. */
export function infrastructureCostsFor(pipeline: Pipeline, now = new Date()) {
  const proof = pipeline.infrastructureCosts;
  const age = proof ? now.getTime() - Date.parse(proof.collectedAt) : Infinity;
  const current = pipeline.quality?.evidenceKind === 'live' && age >= 0 && age <= 26 * 3600_000;
  const candidateStorage = current ? proof?.storage : null;
  const storageAge = candidateStorage ? now.getTime() - Date.parse(candidateStorage.observedAt) : Infinity;
  const storage = storageAge >= 0 && storageAge <= 72 * 3600_000 ? candidateStorage : null;
  const candidateCluster = current ? proof?.cluster : null;
  const clusterAge = candidateCluster ? now.getTime() - Date.parse(candidateCluster.end) : Infinity;
  const cluster = clusterAge >= 0 && clusterAge <= 72 * 3600_000 ? candidateCluster : null;
  const money = (value: string | null, currency = 'USD') => value === null ? "Not measured"
    : new Intl.NumberFormat("en-US",{style:'currency',currency,maximumFractionDigits:4}).format(Number(value));
  const date = (value: string) => new Intl.DateTimeFormat("en-US",{ hourCycle: 'h23',dateStyle:'medium',timeStyle:'short',timeZone:'UTC'}).format(new Date(value))+' UTC';
  return {
    title:"Storage and cluster",
    lines:[
      {label:"S3 Standard storage",value:storage ? new Intl.NumberFormat("en-US",{maximumFractionDigits:2}).format(storage.bytes/1024**3)+" GiB" : "Not measured",detail:storage ? "Observation from "+date(storage.observedAt) : "Daily observation unavailable or old."},
      {label:"S3 per month, at constant volume",value:storage ? money(storage.monthlyRunRate) : "Not measured",detail:storage ? new Intl.NumberFormat("en-US",{maximumFractionDigits:8}).format(Number(storage.pricePerGibMonth))+" USD/GiB-month. Estimate at the observed public AWS rate, first tier. Excludes requests, transfers, discounts and other storage classes." : "An observation and its rate are required for calculation."},
      {label:"Cluster allocation to Quadringent",value:cluster ? money(cluster.allocatedAmount,cluster.currency) : "Not measured",detail:cluster && proof ? 'Namespace '+proof.namespace+' · '+date(cluster.start)+' → '+date(cluster.end) : "Namespace allocation unavailable."},
      {label:"Cluster assets, contextual total",value:cluster ? money(cluster.clusterAmount,cluster.currency) : "Not measured",detail:"OpenCost model: observed nodes, volumes and cluster management."},
      {label:"Unused cluster resources",value:cluster ? money(cluster.idleAmount,cluster.currency) : "Not measured",detail:"Included in the cluster context, not allocated to the namespace."},
    ],
    note:(current && proof ? "Collected on "+date(proof.collectedAt)+'. ' : '')+"Storage covers the site's configured bucket; allocation covers its namespace. Do not add connections that share these resources. OpenCost allocations use the cluster's configured pricing model. Unused resources and shared fees are not allocated to Quadringent. These calculations are not an AWS bill; windows and currencies remain separate.",
  };
}

/* ------------------------------------------------------------------ */
/* Barre de touches de fonction (FunctionKeyBar)                       */
/* ------------------------------------------------------------------ */

/**
 * Les quatre raccourcis du bandeau discret de bas d'écran, en écho aux
 * touches de fonction des terminaux 5250 sans en singer l'esthétique :
 * un mot d'action en français, jamais un jargon technique.
 */
export type FunctionKeyId = 'F3' | 'F5' | 'F9' | 'F12';

export interface FunctionKeyCopy {
  readonly key: FunctionKeyId;
  readonly label: string;
}

export const FUNCTION_KEY_COPY: Readonly<Record<FunctionKeyId, FunctionKeyCopy>> = {
  F3: { key: 'F3', label: "Go back" },
  F5: { key: 'F5', label: "Refresh" },
  F9: { key: 'F9', label: "Pause/Resume" },
  F12: { key: 'F12', label: "Logs" },
};

export function functionKeyCopy(key: FunctionKeyId): FunctionKeyCopy {
  return FUNCTION_KEY_COPY[key];
}

/** Texte de confirmation pour ActionButton — une action destructrice ou
 *  coûteuse ne s'exécute jamais sans qu'on redise ce qu'elle fait. */
export function actionConfirmCopy(label: string): string {
  return `Confirm “${label}”? This action will execute immediately.`;
}

/* ------------------------------------------------------------------ */
/* Assistant de connexion v2 — activation admin, source, Snowflake,    */
/* tables (docs/plans/2026-09-23-control-plane-v2-contract.md §2).     */
/* ------------------------------------------------------------------ */

export const WIZARD_COPY = {
  activate: {
    kicker: "First access",
    title: "Activate your administrator account",
    lead: "This link can be used once. Choose a password to activate your account.",
    passwordLabel: "New password",
    primary: "Activate account",
  },
  source: {
    kicker: "IBM i source",
    title: "Address and account",
    lead: "The specified account will be used to prepare capture. The password is never stored in plaintext.",
    hostLabel: "Address (host or IP)",
    accountLabel: "Account",
    passwordLabel: "Password",
    advanced: "Advanced options (ports)",
    portLabel: 'Port',
    sslPortLabel: "Encrypted port (SSL)",
    test: "Test",
    primary: "Continue",
    trustCertificate: "Trust this certificate",
    // Cette installation n'a pas de sonde IBM i câblée (`SourcesService.test`
    // sans `source_probe`) : le test se limite à vérifier que le secret a
    // été enregistré, jamais présenté comme un test réseau réussi.
    unavailable: "Testing is unavailable on this installation: only password storage has been verified. Network, certificate and authentication have not been tested.",
    existingSourceHint: "A source is already configured. “Test” uses its saved password. Entering a new password creates a new source and retains the previous one.",
  },
  snowflake: {
    resumeLoading: "Reading existing destinations…",
    resumeError: "Loading destinations failed. Try again before creating a new destination.",
    destinationChoice: "Destination to reuse",
    chooseDestination: "Choose a destination",
    declaredState: "Destination configured: access unverified.",
    persistedVerifiedState: "Access verified by the last probe. Verify again before continuing.",
    persistedFailedState: "The last check failed. Verify again after fixing the issue.",
    setupLoading: "Reading the existing script…",
    setupError: "The existing script cannot be loaded. Try again without creating a new key.",
    retry: "Try again",
    destinationRequired: "Choose and verify a Snowflake destination before starting.",

    kicker: "Snowflake destination",
    title: "Your Snowflake account",
    lead: "Enter the account, database and schema that will receive the data. No Snowflake administrator secret is requested here.",
    accountLabel: "Account identifier",
    databaseLabel: "Destination database",
    schemaLabel: "Destination schema (optional)",
    schemaHint: "A specified schema receives both history and mirror. Without a schema, history goes to RAW and the mirror to CURATED.",
    invalidScope: "Invalid identifier: at most 63 characters, starting with a letter or _, followed by letters, digits, _ or $.",
    scriptTitle: "SQL script to run in Snowflake",
    scriptLead: "Run this script with a Snowflake administrator role; it creates a dedicated Quadringent role, service user (authenticated with a key pair) and warehouse.",
    copy: "Copy",
    download: "Download",
    downloadKey: "Download the private key",
    setupNext: "Run the script in Snowflake, then verify access. You can retain a copy of the private key; it will not be provided again.",
    setupNextWithoutKey: "Run the script in Snowflake, then verify access.",
    verify: "Verify access",
    verifying: "Verification in progress…",
    verifyEffect: "This check creates and removes a test table in the destination schemas and may resume the warehouse.",
    verified: "Snowflake access verified. You can choose tables to copy.",
    verificationFailed: "Snowflake access is unverified. Check the script and granted privileges, then try again.",
    keyAlreadyIssued: "The key was provided when the destination was created. It cannot be downloaded again. Use your retained copy.",
    primary: "Continue",
  },
  tables: {
    kicker: "Your tables",
    title: "Choose tables to copy",
    lead: "Each table is verified before starting: IBM i journal and change tracking key.",
    libraries: "IBM i libraries to explore",
    librariesHint: "Enter the libraries containing your tables, separated by commas, then choose Refresh. They may differ from the journal library.",
    librariesInvalid: "Enter at least one library: 1 to 10 letters, digits or _, $, #, @ characters per name, separated by commas.",
    search: "Search for a library or table",
    recheck: "Verify again",
    clPanelTitle: "CL commands to run on IBM i",
    start: "Start",
    keyChoiceLabel: "Change tracking key",
    keyChoiceUniqueIndex: "Configure a key (comma-separated columns)",
    keyChoiceRrn: "Use the physical position (RRN)",
    keyColumnsLabel: "Key columns",
    rrnAcknowledge: "I understand the consequence of this choice.",
  },
} as const;

export function wizardCheckLabel(state: 'ok' | 'attention' | 'failed' | 'unknown'): string {
  switch (state) {
    case 'ok': return 'OK';
    case 'attention': return 'Attention';
    case 'failed': return "Failed";
    case 'unknown': return "Not tested";
  }
}

/* ------------------------------------------------------------------ */
/* Cockpit v2 — panneau de contrôles, réutilisé à chaque niveau        */
/* (table, connexion/source, destination, flotte). Voir                */
/* domain/controlsPanel.ts pour la machine à états ; ce module ne      */
/* porte que le texte.                                                 */
/* ------------------------------------------------------------------ */

export interface ControlActionCopy {
  readonly label: string;
  /** Ce que l'action fait, en une phrase — affiché comme « effet annoncé »
   *  avant confirmation, jamais silencieusement supposé. */
  readonly effect: string;
}

const CONTROL_ACTION_COPY: Readonly<Record<ControlActionKind, ControlActionCopy>> = {
  pause: { label: "Suspend", effect: "Suspends Snowflake delivery. Journal capture continues if other tables remain active." },
  resume: { label: "Resume", effect: "Resumes Snowflake delivery from its last recorded position." },
  restart_initial_copy: { label: "Restart the initial copy", effect: "Restarts the initial copy from the beginning; previously loaded data is replaced." },
  remove: { label: "Remove", effect: "Permanently removes this item from tracking. This action is terminal." },
  replay: { label: "Replay a range", effect: "Replays events from the specified range to the destination." },
};

export function controlActionCopy(action: ControlActionKind): ControlActionCopy {
  return CONTROL_ACTION_COPY[action];
}

const CONTROL_LEVEL_COPY: Readonly<Record<ControlLevel, string>> = {
  table: "this table",
  connection: "all tables in this connection",
  destination: "all tables in this destination",
  fleet: "all tables",
};

/** Titre court du niveau, affiché comme en-tête du panneau de contrôles
 *  (design §3 : « scope title » — un opérateur doit voir d'un coup d'œil à
 *  quelle échelle une action va s'appliquer avant même de la choisir). */
const CONTROL_LEVEL_TITLE: Readonly<Record<ControlLevel, string>> = {
  table: 'Table',
  connection: "Connection",
  destination: 'Destination',
  fleet: "All connections",
};

export function controlLevelTitle(level: ControlLevel): string {
  return CONTROL_LEVEL_TITLE[level];
}

/** Phrase d'effet complète : l'action, à l'échelle du niveau concerné —
 *  jamais une action de flotte présentée comme si elle ne touchait qu'une table. */
export function controlActionAnnouncement(action: ControlActionKind, level: ControlLevel): string {
  const copy = controlActionCopy(action);
  return `${copy.effect} Scope: ${CONTROL_LEVEL_COPY[level]}.`;
}

export const CONTROLS_PANEL_COPY = {
  previewing: "Calculating the effect…",
  confirmTitle: "Confirm action",
  confirm: "Confirm",
  cancel: "Cancel",
  running: "Executing…",
  awaitingConfirmation: "Sensitive action: awaiting approval.",
  approve: "Approve",
  reject: "Reject",
  rerun: "Restart, now approved",
  verifying: "Verifying the effect by reading again…",
  succeeded: "Effect verified by reading again.",
  failedGeneric: "Action not executed.",
} as const;

export const CONFIRMATIONS_COPY = {
  title: 'Confirmations',
  summary: "Pending decisions and approved actions.",
  empty: "No pending decisions",
  emptyDetail: "New requests will appear here.",
  approve: "Approve",
  reject: "Reject",
  execute: "Execute action",
  cancel: "Cancel",
  approveQuestion: "Approve this request?",
  rejectQuestion: "Reject this request?",
  executeQuestion: "Execute this approved action?",
  approveFinal: "Confirm approval",
  rejectFinal: "Confirm rejection",
  executeFinal: "Confirm execution",
  approved: "Request approved. The action has not been executed yet.",
  rejected: "Request rejected. No action was executed.",
  executed: "Action executed and effect verified with the service.",
  executionUnavailable: "This approved action cannot be executed from this screen.",
  changed: "This request is no longer available. The list has been refreshed.",
} as const;

export function confirmationActionLabel(actionRef: string): string {
  switch (actionRef) {
    case 'pipeline.remove': return "Remove the table";
    case 'pipeline.restart_initial_copy': return "Restart the initial copy";
    case 'pipeline.replay': return "Replay events";
    default: return "Sensitive action";
  }
}
