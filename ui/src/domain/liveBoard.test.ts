import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { Overview, Pipeline, Source } from './controlPlane.ts';
import {
  attentionItems,
  deriveThroughput,
  destinationWatermark,
  heartbeat,
  lagVitals,
  observationRows,
  pipelineSectionView,
  plateauVerdict,
  resumeReadiness,
  resumptionView,
  retainedReason,
  runStateWord,
  sectionAlert,
  sourceAvailabilityFor,
  tableRows,
  tableTotals,
  transportFor,
  type CounterSample,
} from './liveBoard.ts';

const observedAt = '2026-09-13T09:00:00Z';
const THIN = ' ';

function pipelineFixture(overrides: Partial<Pipeline> = {}): Pipeline {
  return {
    id: 'alpha',
    environment: 'dev',
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    summary: "State limited to the snapshot",
    observedAt,
    stages: (['source', 'capture', 'raw', 'load', 'destination'] as const).map((id) => ({
      id,
      status: 'healthy' as const,
      observedAt,
      headline: `${id} observé`,
      detail: "Signal receipt.",
    })),
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident: null,
    ...overrides,
  };
}

function sourceFixture(overrides: Partial<Source> = {}): Source {
  return {
    id: 'dev-source',
    environment: 'dev',
    evidenceKind: 'live',
    status: 'available',
    error: null,
    ...overrides,
  };
}

function overviewFixture(
  pipelines: readonly Pipeline[],
  sources: readonly Source[] = [sourceFixture()],
): Overview {
  return {
    revision: 3,
    generatedAt: observedAt,
    scope: { kind: 'single', environments: ['dev'] },
    pipelines,
    sources,
  };
}

function readyState(overview: Overview): ControlPlaneState {
  return {
    status: 'ready',
    connection: 'live',
    overview,
    pipelines: overview.pipelines,
    lastSuccessAt: new Date(observedAt),
  };
}

function lagPoint(lag: number | null, coverage: 'complete' | 'gap' = 'complete') {
  return {
    startSeconds: 0,
    endSeconds: 5,
    low: lag,
    high: lag,
    lag,
    samples: lag === null ? 0 : 1,
    unknownSamples: lag === null ? 1 : 0,
    coverage,
    kind: 'observed' as const,
  };
}

/* ------------------------------------------------------------------ */
/* Débit dérivé                                                        */
/* ------------------------------------------------------------------ */

test('un seul relevé ne produit aucun débit', () => {
  const view = deriveThroughput(undefined, { events: 1793, at: observedAt });
  assert.equal(view.kind, 'single');
  assert.match(view.value, /Cannot calculate — only one observation/);
  assert.equal(view.window, null);
});

test('compteur absent → inconnu, jamais zéro', () => {
  const view = deriveThroughput(
    { events: 100, at: '2026-09-13T08:59:30Z' },
    { events: null, at: observedAt },
  );
  assert.equal(view.kind, 'unknown');
  assert.match(view.value, /Unknown — counter not published/);
});

test('delta nul → 0 événement avec fenêtre affichée', () => {
  const view = deriveThroughput(
    { events: 1793, at: '2026-09-13T08:59:30Z' },
    { events: 1793, at: observedAt },
  );
  assert.equal(view.kind, 'idle');
  assert.equal(view.value, "0 events");
  assert.equal(view.window, `window 30${THIN}s`);
});

test('débit mesuré = Δ events_published / Δ temps', () => {
  const view = deriveThroughput(
    { events: 1_000, at: '2026-09-13T08:59:30Z' },
    { events: 1_300, at: observedAt },
  );
  assert.equal(view.kind, 'measured');
  assert.equal(view.value, "10 events/s");
  assert.equal(view.window, `window 30${THIN}s`);
  assert.match(view.note ?? '', /Δ \+300/);
});

test('régression du compteur = redémarrage observé avec fenêtre', () => {
  const view = deriveThroughput(
    { events: 500_000, at: '2026-09-13T08:59:30Z' },
    { events: 12, at: observedAt },
  );
  assert.equal(view.kind, 'restarted');
  assert.match(view.value, /Restart observed/);
  assert.equal(view.window, `window 30${THIN}s`);
  assert.match(view.note ?? '', /restarted at 12/);
});

test('deux relevés au même instant ne divisent pas par zéro', () => {
  const view = deriveThroughput(
    { events: 100, at: observedAt },
    { events: 200, at: observedAt },
  );
  assert.equal(view.kind, 'unknown');
  assert.match(view.value, /same time/);
});

test('compteur absent du relevé précédent borne le calcul', () => {
  const view = deriveThroughput(
    { events: null, at: '2026-09-13T08:59:30Z' },
    { events: 50, at: observedAt },
  );
  assert.equal(view.kind, 'unknown');
  assert.equal(view.window, `window 30${THIN}s`);
});

/* ------------------------------------------------------------------ */
/* État de run en un mot                                               */
/* ------------------------------------------------------------------ */

test('le run word garde le dernier état mesuré quand le relevé est retenu', () => {
  const word = runStateWord(pipelineFixture(), { retained: "retained observation", current: false });
  // Le gel ne rend pas la vérité inconnue : le dernier mot mesuré reste
  // servi, désaturé — la raison de rétention vit dans le badge de portée
  // et l'âge dans le heartbeat.
  assert.equal(word.label, 'Capture active');
  assert.equal(word.tone, 'muted');
});

test('un relevé retenu sans exécution projetée reste Inconnu — raison nommée', () => {
  const word = runStateWord(
    pipelineFixture({
      stages: (['source', 'capture', 'raw', 'load', 'destination'] as const).map((id) => ({
        id,
        status: 'unknown' as const,
        observedAt: null,
        headline: "not observed",
        detail: 'absente',
      })),
    }),
    { retained: "retained observation", current: false },
  );
  assert.equal(word.label, "Unknown — execution not projected");
  assert.equal(word.tone, 'muted');
});

test('incident capture_stopped → arrêtée en sécurité, ton bleu-gris pas rouge', () => {
  const word = runStateWord(
    pipelineFixture({ incident: { code: 'capture_stopped_fail_closed', type: 'capture_stopped' } }),
    { retained: null, current: true },
  );
  assert.equal(word.label, "Stopped safely");
  assert.equal(word.tone, 'muted');
});

test('phase LIVE mesurée → Capture active en positif', () => {
  const word = runStateWord(
    pipelineFixture({
      fleetRuntime: {
        formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
        phase: 'LIVE', checkpoint: null, capabilities: {}, tableStates: [],
      },
    }),
    { retained: null, current: true },
  );
  assert.equal(word.label, 'Capture active');
  assert.equal(word.tone, 'positive');
});

test('même phase LIVE en démonstration reste bornée', () => {
  const word = runStateWord(
    pipelineFixture({
      quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'simulation' },
      fleetRuntime: {
        formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
        phase: 'LIVE', checkpoint: null, capabilities: {}, tableStates: [],
      },
    }),
    { retained: null, current: false },
  );
  assert.equal(word.label, "Capture active · demonstration");
  assert.equal(word.tone, 'muted');
});

test('planned_stop → En pause ; PAUSED runtime → En pause ; BLOCKED → Bloquée', () => {
  const paused = runStateWord(pipelineFixture({ status: 'planned_stop' }), {
    retained: null,
    current: true,
  });
  assert.equal(paused.label, "Paused");

  const runtimePaused = runStateWord(
    pipelineFixture({
      fleetRuntime: {
        formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
        phase: 'PAUSED', checkpoint: null, capabilities: {}, tableStates: [],
      },
    }),
    { retained: null, current: true },
  );
  assert.equal(runtimePaused.label, "Paused");

  const blocked = runStateWord(
    pipelineFixture({
      fleetRuntime: {
        formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
        phase: 'BLOCKED', checkpoint: null, capabilities: {}, tableStates: [],
      },
    }),
    { retained: null, current: true },
  );
  assert.equal(blocked.label, "Blocked");
  assert.equal(blocked.tone, 'attention');
});

test('sans runtime, l étape capture suffit à dire Capture active', () => {
  const word = runStateWord(pipelineFixture(), { retained: null, current: true });
  assert.equal(word.label, 'Capture active');
  assert.equal(word.tone, 'positive');
});

test('sans runtime ni capture observée → Inconnu, jamais de succès', () => {
  const pipeline = pipelineFixture({
    stages: pipelineFixture().stages.map((stage) =>
      stage.id === 'capture' ? { ...stage, status: 'unknown' as const } : stage,
    ),
  });
  const word = runStateWord(pipeline, { retained: null, current: true });
  assert.match(word.label, /^Unknown — /);
});

/* ------------------------------------------------------------------ */
/* Rétention et disponibilité                                          */
/* ------------------------------------------------------------------ */

test('retainedReason ordonne gelé > conservé > fraîcheur > source', () => {
  const pipeline = pipelineFixture({ quality: { coverage: 'complete', freshness: 'stale', evidenceKind: 'live' } });
  const availability = sourceAvailabilityFor(pipeline, [sourceFixture()]);
  assert.equal(retainedReason(pipeline, 'frozen', availability), "refresh frozen");
  assert.equal(retainedReason(pipeline, 'retained', availability), "retained observation");
  assert.equal(retainedReason(pipeline, 'live', availability), "observation too old");
});

test('source indisponible ou partielle fige la lecture', () => {
  const pipeline = pipelineFixture();
  const down = sourceAvailabilityFor(pipeline, [sourceFixture({ status: 'unavailable' })]);
  assert.equal(down.kind, 'unavailable');
  assert.equal(retainedReason(pipeline, 'live', down), "source unavailable");

  const partial = sourceAvailabilityFor(pipeline, [
    sourceFixture({ id: 'a' }),
    sourceFixture({ id: 'b', status: 'unavailable' }),
  ]);
  assert.equal(partial.kind, 'partial');
  assert.match(partial.label, /Partial · 1\/2/);
  assert.equal(retainedReason(pipeline, 'live', partial), "some sources unavailable");
});

test('aucune source déclarée pour l environnement → inconnue', () => {
  const availability = sourceAvailabilityFor(pipelineFixture(), []);
  assert.equal(availability.kind, 'unmapped');
  assert.match(availability.label, /no source configured/);
});

test('transportFor distingue live, relevé conservé et gel', () => {
  const state = readyState(overviewFixture([pipelineFixture()]));
  assert.equal(transportFor(state, false), 'live');
  assert.equal(transportFor(state, true), 'frozen');
  assert.equal(
    transportFor({ ...state, status: 'degraded', message: 'offline' }, false),
    'retained',
  );
  assert.equal(transportFor({ ...state, connection: 'reconnecting' }, false), 'retained');
});

/* ------------------------------------------------------------------ */
/* Grille des tables                                                   */
/* ------------------------------------------------------------------ */

function planTable(
  name: string,
  overrides: Record<string, unknown> = {},
): NonNullable<Pipeline['fleetPlan']>['tables'][number] {
  return {
    name,
    rowCount: 1000,
    dataSize: 10,
    journalImages: '*BOTH',
    identityStatus: 'keyed',
    identitySource: null,
    candidateKey: null,
    historicalAdmitted: true,
    historicalLane: null,
    blockedReasons: [],
    copiedRows: null,
    copiedBytes: null,
    historyProgress: null,
    ...overrides,
  };
}

function planPipeline(): Pipeline {
  return pipelineFixture({
    fleetPlan: {
      environment: 'TEST',
      sourceSchema: 'LEDGER',
      destinationNamespace: 'ACME_RAW.IBMI_TEST',
      observedAt,
      freshness: 'fresh',
      continuity: 'uncertain',
      liveBlocked: false,
      certificationBlocked: false,
      historyAdmitted: true,
      cutoverCheckpoint: { receiver: 'TRNJRN3776', sequence: 1 },
      cutoverRequiredBeforeHistory: true,
      journal: { library: 'LEDGER', name: 'TRNJRN', readerKind: 'multi_object', readerCount: 1 },
      identity: { keyedCount: 2, rrnCount: 0, blockedCount: 1, keyed: ['SALE', 'CAL001'], rrn: [], blocked: ['ADDRS1'] },
      observedTotals: { tableCount: 3, rowCount: 3000, dataSize: 30 },
      historical: { maxConcurrency: 2, byteBudget: null, admittedCount: 2, excludedCount: 1 },
      cost: { status: 'unknown', observed: null, unknownBecause: 'not measured' },
      tables: [
        planTable('ADDRS1', { identityStatus: 'blocked', blockedReasons: ['identity_unproven'] }),
        planTable('CAL001'),
        planTable('SALE'),
      ],
    },
    fleetRuntime: {
      formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
      phase: 'LIVE',
      checkpoint: { receiver: 'TRNJRN3776', sequence: 345_089_832 },
      capabilities: {},
      tableStates: [
        { name: 'ADDRS1', phase: 'BLOCKED', copiedRows: null, totalRows: null },
        { name: 'CAL001', phase: 'CERTIFIED', copiedRows: 1000, totalRows: 1000 },
        { name: 'SALE', phase: 'LIVE', copiedRows: 800, totalRows: 1000 },
      ],
    },
  });
}

test('les lignes fusionnent runtime et plan : problèmes en tête puis phase décroissante', () => {
  const rows = tableRows(planPipeline(), true);
  assert.equal(rows.length, 3);
  assert.equal(rows[0]!.name, 'ADDRS1');
  assert.equal(rows[0]!.problem, 'blocked');
  assert.match(rows[0]!.verdict.label, /Blocked — identity not proven/);
  assert.equal(rows[1]!.name, 'CAL001');
  assert.equal(rows[1]!.verdict.label, "Verified");
  assert.equal(rows[1]!.verdict.tone, 'positive');
  assert.equal(rows[2]!.name, 'SALE');
  assert.equal(rows[2]!.verdict.label, "Real time");
  assert.equal(rows[2]!.progress, 0.8);
});

test('journal *BEFORE est un problème, pas une note de bas de page', () => {
  const pipeline = planPipeline();
  pipeline.fleetPlan!.tables[1]!.journalImages;
  const mutated = {
    ...pipeline,
    fleetPlan: {
      ...pipeline.fleetPlan!,
      tables: pipeline.fleetPlan!.tables.map((table) =>
        table.name === 'SALE' ? { ...table, journalImages: '*BEFORE' as const } : table,
      ),
    },
  };
  const rows = tableRows(mutated, true);
  assert.equal(rows[0]!.name, 'ADDRS1');
  assert.equal(rows[1]!.name, 'SALE');
  assert.equal(rows[1]!.problem, 'journal');
  assert.equal(rows[1]!.verdict.label, "Unusable images");
});

test('hors mesure live, le vert disparaît des verdicts de table', () => {
  const rows = tableRows(planPipeline(), false);
  const certified = rows.find((row) => row.name === 'CAL001')!;
  assert.equal(certified.verdict.label, "Verified");
  assert.equal(certified.verdict.tone, 'muted');
});

test('compteurs null → Inconnu, jamais zéro affiché ; le total catalogue reste distinct', () => {
  const rows = tableRows(planPipeline(), true);
  const blocked = rows[0]!;
  assert.equal(blocked.copiedRows, null);
  assert.equal(blocked.totalRows, 1000);
  assert.equal(blocked.progress, null);
});

test('phase absente du runtime mais table admise → inconnue honnête', () => {
  const pipeline = planPipeline();
  const mutated = {
    ...pipeline,
    fleetRuntime: {
      ...pipeline.fleetRuntime!,
      tableStates: pipeline.fleetRuntime!.tableStates.filter((t) => t.name !== 'SALE'),
    },
  };
  const rows = tableRows(mutated, true);
  const orphan = rows.find((row) => row.name === 'SALE')!;
  assert.equal(orphan.phase, null);
  assert.equal(orphan.verdict.label, "Unknown — phase not measured");
});

/* ------------------------------------------------------------------ */
/* Retard                                                              */
/* ------------------------------------------------------------------ */

test('lag null → inconnu ; trous de lag_series comptés honnêtement', () => {
  const pipeline = pipelineFixture({
    lagSeries: [
      lagPoint(4), lagPoint(3), lagPoint(null, 'gap'),
      { ...lagPoint(null), kind: 'temporal_gap' as const },
      lagPoint(2), lagPoint(2),
    ],
  });
  const view = lagVitals(pipeline);
  assert.match(view.value, /Unknown — not measured/);
  assert.equal(view.gaps, "2 intervals without measurement");
});

test('lag mesuré garde le verdict canonique', () => {
  const pipeline = pipelineFixture({
    lagSequences: 2,
    lagSeries: [lagPoint(5), lagPoint(3), lagPoint(2), lagPoint(1)],
  });
  const view = lagVitals(pipeline);
  assert.match(view.value, /2 sequences of lag/);
  assert.equal(view.verdict, "Catching up");
});

/* ------------------------------------------------------------------ */
/* Vue de section + verdict global + file d attention                  */
/* ------------------------------------------------------------------ */

test('la section expose checkpoint, débit et badges sans invention', () => {
  const pipeline = planPipeline();
  pipeline.counters = { events_published: 345_000 };
  const view = pipelineSectionView(pipeline, {
    sources: [sourceFixture()],
    transport: 'live',
    previous: { events: 340_000, at: '2026-09-13T08:59:30Z' } satisfies CounterSample,
  });
  assert.equal(view.retained, null);
  assert.equal(view.current, true);
  assert.equal(view.runWord.label, 'Capture active');
  assert.deepEqual(view.checkpoint, { receiver: 'TRNJRN3776', sequence: 345_089_832 });
  assert.match(view.throughput.value, /events\/s/);
  assert.equal(view.throughput.window, `window 30${THIN}s`);
  assert.equal(view.rows.length, 3);
});

test('checkpoint absent → raison honnête', () => {
  const view = pipelineSectionView(pipelineFixture(), {
    sources: [sourceFixture()],
    transport: 'live',
    previous: undefined,
  });
  assert.equal(view.checkpoint, null);
  assert.equal(view.checkpointMissingReason, "execution not projected");
});

test('verdict global : ça avance / ça coince / on ne sait pas', () => {
  const healthy = pipelineFixture({
    fleetRuntime: {
      formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
      phase: 'LIVE', checkpoint: null, capabilities: {}, tableStates: [],
    },
  });
  const advancing = plateauVerdict(readyState(overviewFixture([healthy])), 'live');
  assert.equal(advancing.label, "Progressing");

  const incident = pipelineFixture({
    status: 'incident',
    incident: { code: 'destination_load_failed', type: 'destination' },
  });
  const stuck = plateauVerdict(readyState(overviewFixture([incident])), 'live');
  assert.equal(stuck.label, "Blocked");
  assert.match(stuck.detail, /alpha/);

  const unknown = plateauVerdict(readyState(overviewFixture([pipelineFixture()])), 'retained');
  assert.equal(unknown.label, "Unknown");

  const loading = plateauVerdict(
    { status: 'loading', connection: 'connecting' },
    'retained',
  );
  assert.equal(loading.label, "Unknown");
  assert.match(loading.detail, /first observation/);
});

/* ------------------------------------------------------------------ */
/* Heartbeat, bandeau, watermark, totaux, journal                        */
/* ------------------------------------------------------------------ */

test('heartbeat mesure le canal et l âge du relevé, jamais « en direct »', () => {
  const now = new Date('2026-09-13T09:00:30Z');
  const beat = heartbeat(pipelineFixture(), 'live', now);
  assert.match(beat.measure, /^observation 30\ss ago$/);
  assert.equal(beat.channel, "measurement channel open");
  assert.equal(beat.stale, false);
  assert.equal(beat.tone, 'active');

  const stale = heartbeat(
    pipelineFixture({ quality: { coverage: 'complete', freshness: 'stale', evidenceKind: 'live' } }),
    'live', now,
  );
  assert.match(stale.measure, /^observations frozen since 30\ss ago$/);
  assert.equal(stale.channel, "measurement channel open");
  assert.equal(stale.tone, 'attention');

  // Un relevé frais déclaré mais vieux de plus de 120 s se lit figé — honnête
  const old = heartbeat(pipelineFixture(), 'live', new Date('2026-09-13T09:05:00Z'));
  assert.match(old.measure, /observations frozen since/);

  assert.equal(heartbeat(pipelineFixture(), 'retained', now).channel, "measurement channel interrupted");
  assert.match(heartbeat(pipelineFixture(), 'frozen', now).channel, /paused/);
});

test('heartbeat : la révision servie prouve la boucle de lecture', () => {
  const now = new Date('2026-09-13T09:00:30Z');
  const generated = { revision: 41, generatedAt: '2026-09-13T09:00:22Z', received: 12 };
  const beat = heartbeat(pipelineFixture(), 'live', now, generated);
  // « révision 41 · générée il y a 8 s · 12 relevés reçus » — mesuré, la
  // boucle est démontrée par le compteur vivant, pas par le jargon « canal »
  assert.match(beat.channel, /^revision 41 · generated 8\ss ago · 12 observations received$/);
  // Canal fermé : la révision reste affichée (dernier relevé lu), l'interruption le dit
  const closed = heartbeat(pipelineFixture(), 'retained', now, generated);
  assert.match(closed.channel, /revision 41 · generated 8\ss ago · measurement channel interrupted/);
  // Affichage suspendu par l'opérateur : les relevés reçus restent visibles, la suspension est dite
  const frozen = heartbeat(pipelineFixture(), 'frozen', now, generated);
  assert.match(frozen.channel, /12 observations received · display paused/);
  // Un seul relevé reçu : accord au singulier
  const first = heartbeat(pipelineFixture(), 'live', now, { ...generated, received: 1 });
  assert.match(first.channel, /1 observation received$/);
});

test('sectionAlert traduit l incident en français avec remède guidance', () => {
  const alert = sectionAlert(
    pipelineFixture({ incident: { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked' } }),
    { transport: 'live', retained: null },
  );
  assert.equal(alert.severity, 'incident');
  assert.equal(alert.locus, "Reader");
  assert.equal(alert.headline, "AS400 authentication blocked");
  assert.match(alert.cause, /the source rejects the connection/);
  assert.match(alert.remedy, /Fix the AS400 account, then restart/);
  assert.equal(alert.observedAt, observedAt);
  // L'incident destination nomme la destination, pas la lecture
  const destination = sectionAlert(
    pipelineFixture({ incident: { code: 'destination_reconciliation_mismatch', type: 'destination' } }),
    { transport: 'live', retained: null },
  );
  assert.equal(destination.locus, 'Destination');
});

test('sectionAlert : un seul bandeau — incident d abord, sinon relevé dégradé', () => {
  assert.equal(
    sectionAlert(pipelineFixture(), { transport: 'live', retained: null }),
    null,
  );
  // L affichage suspendu par l opérateur n est pas une alerte
  assert.equal(
    sectionAlert(pipelineFixture(), { transport: 'frozen', retained: "refresh frozen" }),
    null,
  );
  const stale = sectionAlert(
    pipelineFixture(),
    { transport: 'live', retained: "observation too old" },
  );
  assert.equal(stale.severity, 'degrade');
  assert.equal(stale.locus, null);
  assert.match(stale.cause, /observation too old/);
  const cached = sectionAlert(
    pipelineFixture(),
    { transport: 'retained', retained: "retained observation" },
  );
  assert.match(cached.cause, /measurement channel is interrupted/);
});

test('resumptionView : reprise sans faux espoir, flux mesuré, dernier débit ou non mesuré', () => {
  const pipeline = planPipeline();
  pipeline.counters = { events_published: 345_000, events_in_target: 342_580 };
  const view = resumptionView(pipeline, { value: "10 events/s", at: '2026-09-12T12:00:00Z' });
  assert.match(view.retry, /no new attempt observed — intervention required/);
  assert.match(view.flow, /journal TRNJRN3776\s·\s345,089,832 → 345,000 published → 342,580 at the destination/);
  assert.match(view.lastRate, /10 events\/s/);

  const bare = resumptionView(pipelineFixture(), null);
  assert.match(bare.flow, /journal not measured → published count not measured → destination not measured/);
  assert.match(bare.lastRate, /not measured/);
});

test('la vue de section expose la boucle de reprise seulement sur incident', () => {
  const incident = pipelineFixture({ incident: { code: 'x', type: 'capture_auth_blocked' } });
  const view = pipelineSectionView(incident, {
    sources: [sourceFixture()],
    transport: 'live',
    previous: undefined,
  });
  assert.notEqual(view.resume, null);
  const healthy = pipelineSectionView(pipelineFixture(), {
    sources: [sourceFixture()],
    transport: 'live',
    previous: undefined,
  });
  assert.equal(healthy.resume, null);
});

test('destinationWatermark : compteur constaté, namespace seul, ou non instrumenté', () => {
  const pipeline = planPipeline();
  pipeline.counters = { events_in_target: 342_580 };
  const watermark = destinationWatermark(pipeline);
  assert.match(watermark.value, /342,580 events observed/);
  assert.match(watermark.hint, /ACME_RAW\.IBMI_TEST · observed total — not a reconciliation/);

  pipeline.counters = {};
  const nsOnly = destinationWatermark(pipeline);
  assert.equal(nsOnly.value, 'ACME_RAW.IBMI_TEST');
  assert.equal(nsOnly.hint, "delivery counter not served");

  const bare = destinationWatermark(pipelineFixture());
  assert.equal(bare.value, "Destination · undated");
});

test('tableTotals : compte catalogue, copiées mesurées et problèmes, sans invention', () => {
  const rows = tableRows(planPipeline(), true);
  const totals = tableTotals(planPipeline(), rows);
  assert.equal(totals.tableCount, 3);
  assert.equal(totals.cataloguedRows, 3000);
  assert.equal(totals.copiedRows, 1800);
  assert.equal(totals.problems, 1);

  const empty = tableTotals(pipelineFixture(), []);
  assert.equal(empty.tableCount, 0);
  assert.equal(empty.cataloguedRows, null);
  assert.equal(empty.copiedRows, null);
});

test('observationRows : relevé, catalogue et étapes déduits — rien d inventé', () => {
  const rows = observationRows(planPipeline());
  assert.equal(rows.length, 2);
  assert.equal(rows[0]!.kind, 'releve');
  assert.match(rows[0]!.label, /Observation read — State limited to the snapshot/);
  assert.equal(rows[1]!.kind, 'catalogue');
  assert.match(rows[1]!.label, /Catalog observed — 3,000 rows across 3 tables · continuity uncertain/);

  const degraded = pipelineFixture({
    stages: pipelineFixture().stages.map((stage) =>
      stage.id === 'destination' ? { ...stage, status: 'unknown' as const, observedAt: null } : stage,
    ),
  });
  const labels = observationRows(degraded).map((row) => row.label);
  assert.ok(labels.some((label) => /Destination — never observed/.test(label)));
});

test('la cellule État fusionnée ne répète le verdict que s il diverge', () => {
  const rows = tableRows(planPipeline(), true);
  const blocked = rows.find((row) => row.name === 'ADDRS1')!;
  assert.equal(blocked.state.primary, "Blocked — identity not proven");
  assert.equal(blocked.state.secondary, null);
  const certified = rows.find((row) => row.name === 'CAL001')!;
  assert.equal(certified.state.primary, "Verified");
  assert.equal(certified.state.secondary, null);
});

test('volumeShare : la barre encode le volume relatif de la plus grande table', () => {
  const pipeline = planPipeline();
  const mutated = {
    ...pipeline,
    fleetRuntime: {
      ...pipeline.fleetRuntime!,
      tableStates: [
        { name: 'ADDRS1', phase: 'BLOCKED' as const, copiedRows: null, totalRows: 2000 },
        { name: 'CAL001', phase: 'CERTIFIED' as const, copiedRows: 500, totalRows: 1000 },
        { name: 'SALE', phase: 'LIVE' as const, copiedRows: 500, totalRows: 500 },
      ],
    },
  };
  const rows = tableRows(mutated, true);
  assert.equal(rows.find((row) => row.name === 'ADDRS1')!.volumeShare, 1);
  assert.equal(rows.find((row) => row.name === 'CAL001')!.volumeShare, 0.5);
  assert.equal(rows.find((row) => row.name === 'SALE')!.volumeShare, 0.25);
});

test('la file d attention épingle les ruptures, jamais les pipelines sains', () => {
  const healthy = pipelineFixture({ id: 'sain' });
  const broken = pipelineFixture({
    id: 'en-rupture',
    stages: pipelineFixture().stages.map((stage) =>
      stage.id === 'destination' ? { ...stage, status: 'unknown' as const, observedAt: null } : stage,
    ),
  });
  const items = attentionItems(readyState(overviewFixture([healthy, broken])));
  assert.equal(items.length, 1);
  assert.equal(items[0]!.pipeline.id, 'en-rupture');
  assert.match(items[0]!.cause, /./);
});

/* ------------------------------------------------------------------ */
/* « Prête à reprendre » — cause résolue prouvée, capture parquée       */
/* ------------------------------------------------------------------ */

function authBlockedPipeline(overrides: Partial<Pipeline> = {}): Pipeline {
  const base = planPipeline();
  return {
    ...base,
    status: 'incident',
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked' },
    fleetPlan: { ...base.fleetPlan!, continuity: 'proven' as const, freshness: 'fresh' as const },
    stages: base.stages.map((stage) =>
      stage.id === 'capture'
        ? { ...stage, status: 'incident' as const, headline: "Capture stopped safely" }
        : stage,
    ),
    ...overrides,
  };
}

test('reprise prête : incident récupérable + plan frais + continuité prouvée', () => {
  assert.equal(resumeReadiness(authBlockedPipeline()), 'ready');
});

test('reprise refusée : relevé de plan trop ancien ne prouve rien', () => {
  const pipeline = authBlockedPipeline();
  const stale = {
    ...pipeline,
    fleetPlan: { ...pipeline.fleetPlan!, freshness: 'stale' as const },
  };
  assert.equal(resumeReadiness(stale), null);
});

test('reprise refusée : continuité non prouvée ne qualifie pas', () => {
  const pipeline = authBlockedPipeline();
  const uncertain = {
    ...pipeline,
    fleetPlan: { ...pipeline.fleetPlan!, continuity: 'uncertain' as const },
  };
  assert.equal(resumeReadiness(uncertain), null);
});

test('reprise refusée : une capture qui tourne ne se qualifie pas', () => {
  const pipeline = authBlockedPipeline({ status: 'healthy' });
  assert.equal(resumeReadiness(pipeline), null);
});

test('reprise refusée : un incident destination n est pas récupérable par la sonde', () => {
  const pipeline = authBlockedPipeline({
    incident: { code: 'destination_apply_failed', type: 'destination' },
  });
  assert.equal(resumeReadiness(pipeline), null);
});

test('reprise prête : le drapeau serveur cause_resolved suffit sans plan', () => {
  const pipeline = authBlockedPipeline({
    fleetPlan: null,
    incident: {
      code: 'capture_stopped_fail_closed',
      type: 'capture_auth_blocked',
      causeResolved: true,
      causeResolvedObservedAt: '2026-09-21T14:07:49Z',
    },
  });
  assert.equal(resumeReadiness(pipeline), 'ready');
});

test('reprise prête : le bloc resume servi prime sur la dérivation', () => {
  const pipeline = authBlockedPipeline({
    fleetPlan: null,
    status: 'awaiting_resume',
    resume: {
      state: 'ready',
      authObservedAt: '2026-09-21T14:07:49Z',
      checkpoint: { receiver: 'DEMOJRN4114', sequence: 345_089_832 },
      tail: { receiver: 'DEMOJRN4158', sequence: 120_332_519 },
      backlogSequences: 77_777,
      backlogReceivers: 44,
    },
  });
  assert.equal(resumeReadiness(pipeline), 'ready');
});

test('alerte prête : cause résolue en titre, jamais incident actif', () => {
  const alert = sectionAlert(authBlockedPipeline(), { transport: 'live', retained: null });
  assert.equal(alert?.ready, true);
  assert.equal(alert?.headline, "Cause resolved");
  assert.equal(alert?.tone, 'attention');
  assert.match(alert?.cause ?? '', /accepts connections again/);
});

test('alerte incident actif : inchangée quand la cause n est pas résolue', () => {
  const pipeline = authBlockedPipeline({
    fleetPlan: { ...authBlockedPipeline().fleetPlan!, freshness: 'stale' as const },
  });
  const alert = sectionAlert(pipeline, { transport: 'live', retained: null });
  assert.equal(alert?.ready, false);
});

test('le mot de run prête à reprendre ne devient jamais un incident rouge', () => {
  const word = runStateWord(authBlockedPipeline(), { retained: null, current: false });
  assert.equal(word.label, "Ready to resume");
  assert.equal(word.tone, 'attention');
});

test('vue reprise prête : point d arrêt mesuré, pas de ligne de débit vide', () => {
  const view = resumptionView(authBlockedPipeline(), null);
  assert.equal(view.retry, null);
  assert.match(view.flow, /Restart from TRNJRN3776/);
  assert.equal(view.lastRate, null);
});

test('vue reprise prête : backlog mesuré servi tel quel', () => {
  const pipeline = authBlockedPipeline({
    resume: {
      state: 'ready',
      authObservedAt: '2026-09-21T14:07:49Z',
      checkpoint: { receiver: 'DEMOJRN4114', sequence: 345_089_832 },
      tail: { receiver: 'DEMOJRN4158', sequence: 120_332_519 },
      backlogSequences: 77_777,
      backlogReceivers: 44,
    },
  });
  const view = resumptionView(pipeline, null);
  assert.match(view.flow, new RegExp(`77,777 sequences to reread`));
});

test('plateau : une source prête à reprendre dit À reprendre, pas Ça coince', () => {
  const state = readyState(overviewFixture([authBlockedPipeline()]));
  const verdict = plateauVerdict(state, 'live');
  assert.equal(verdict.label, "To resume");
  assert.match(verdict.detail, /cause resolved/);
});
