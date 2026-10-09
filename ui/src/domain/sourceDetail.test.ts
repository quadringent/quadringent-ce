import assert from 'node:assert/strict';
import { test } from 'node:test';

import type {
  DestinationProjection,
  FluxIdentity,
  JournalCheckpoint,
  JournalPosition,
  Pipeline,
  RunProjection,
} from './controlPlane.ts';
import {
  NOT_INSTRUMENTED,
  checkpointGlyph,
  clockUtc,
  destinationApplyStamp,
  destinationDetailSections,
  destinationReasonLabel,
  destinationRows,
  destinationStamp,
  fluxIdentityRows,
  fluxObjectCoverage,
  positionGlyph,
  positionRows,
  projectedLagVerdict,
  runDetailRows,
  runStateLabel,
  stageChain,
} from './sourceDetail.ts';

const observedAt = '2026-09-13T09:00:00Z';
const now = new Date('2026-09-13T09:00:30Z');
const THIN = ' ';

function pipelineFixture(overrides: Partial<Pipeline> = {}): Pipeline {
  return {
    id: 'dev-cntr',
    environment: 'dev',
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    summary: "State borné au observation",
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

function positionFixture(overrides: Partial<JournalPosition> = {}): JournalPosition {
  return {
    checkpoint: { receiver: 'TRNJRN3776', sequence: 41 },
    sourceTail: { receiver: 'TRNJRN3776', sequence: 1337 },
    receiverFirstSequence: 40,
    receiverLastSequence: 1337,
    ...overrides,
  };
}

function fluxFixture(overrides: Partial<FluxIdentity> = {}): FluxIdentity {
  return {
    id: 'dev-cntr',
    label: 'CNTR',
    journal: 'QJRN',
    journalLibrary: 'ACME_LIB',
    objects: ['CNTR', "ACME.CUSTOMERS"],
    readerPath: 'journal',
    target: 'snowflake',
    job: 'CDCREADER1',
    ...overrides,
  };
}

function runFixture(overrides: Partial<RunProjection> = {}): RunProjection {
  return {
    state: 'RUNNING',
    startedAt: '2026-09-13T08:00:00Z',
    elapsedSeconds: 3600,
    stoppedBecause: null,
    diagnostic: { type: 'budget', head: 'quota atteint', at: '2026-09-13T08:30:00Z' },
    sourcePause: { retryAfter: '2026-09-13T10:00:00Z', reasonCode: 'journal_wrap' },
    ...overrides,
  };
}

function destinationFixture(overrides: Partial<DestinationProjection> = {}): DestinationProjection {
  return {
    kind: 'snowflake',
    database: 'ACME_RAW',
    schema: 'DEV',
    stage: 'QS_STAGE',
    rawTable: 'CNTR_RAW',
    canonicalTable: 'CNTR',
    runTag: 'run-41',
    observedAt: '2026-09-13T09:42:13Z',
    loadCheckpoint: { receiver: 'snowpipe', sequence: 900 },
    applyCheckpoint: { receiver: 'snowpipe', sequence: 901 },
    sourceEvents: 120,
    rawRows: 120,
    canonicalRows: 118,
    duplicates: 2,
    ...overrides,
  };
}

/* ------------------------------------------------------------------ */
/* Horodatage et glyphes — HH:MM:SS UTC, jamais d'heure locale          */
/* ------------------------------------------------------------------ */

test('clockUtc renders the UTC wall clock, never a local time', () => {
  assert.equal(clockUtc('2026-09-13T09:42:13Z'), '09:42:13');
  assert.equal(clockUtc('2026-09-13T23:59:59+00:00'), '23:59:59');
});

test('checkpointGlyph joins receiver and sequence in canonical form', () => {
  assert.equal(checkpointGlyph({ receiver: 'TRNJRN3776', sequence: 41 }), 'TRNJRN3776:41');
  assert.equal(checkpointGlyph({ receiver: 'TRNJRN3776', sequence: 1337 }), `TRNJRN3776:1,337`);
});

/* ------------------------------------------------------------------ */
/* Glyphe position — absent / partiel / mesuré, trois cas distincts     */
/* ------------------------------------------------------------------ */

test('position glyph is uninstrumented when the extension is absent', () => {
  const glyph = positionGlyph(pipelineFixture());
  assert.equal(glyph.instrumented, false);
  assert.equal(glyph.value, NOT_INSTRUMENTED);
  assert.match(glyph.reason ?? '', /not measured/);
  assert.equal(glyph.window, null);
});

test('position glyph uses the runtime checkpoint without inventing a tail', () => {
  const checkpoint: JournalCheckpoint = { receiver: 'TRNJRN3776', sequence: 41 };
  const glyph = positionGlyph(pipelineFixture({
    fleetRuntime: { checkpoint } as Pipeline['fleetRuntime'],
  }));
  assert.equal(glyph.instrumented, false);
  assert.equal(glyph.value, "TRNJRN3776:41 → end unknown");
  assert.match(glyph.reason ?? '', /journal end not measured/);
});

test('position glyph renders the served checkpoint→tail form with the receiver window', () => {
  const glyph = positionGlyph(pipelineFixture({ position: positionFixture() }));
  assert.equal(glyph.instrumented, true);
  assert.equal(glyph.value, `TRNJRN3776:41 → TRNJRN3776:1,337`);
  assert.equal(glyph.window, `sequences 40 → 1,337`);
  assert.equal(glyph.reason, null);
});

test('position glyph names a missing bound instead of completing it', () => {
  const glyph = positionGlyph(pipelineFixture({
    position: positionFixture({ sourceTail: null, receiverFirstSequence: null, receiverLastSequence: null }),
  }));
  assert.equal(glyph.instrumented, true);
  assert.equal(glyph.value, "TRNJRN3776:41 → end unknown");
  assert.equal(glyph.window, null);
  assert.equal(glyph.reason, "boundary missing from the observation");
});

test('position glyph stays honest when no bound is published', () => {
  const glyph = positionGlyph(pipelineFixture({
    position: positionFixture({ checkpoint: null, sourceTail: null }),
  }));
  assert.equal(glyph.instrumented, true);
  assert.equal(glyph.value, "Unknown — boundaries not published");
});

test('position rows distinguish absent extension, runtime fallback and served bounds', () => {
  const absent = positionRows(pipelineFixture());
  assert.equal(absent.length, 3);
  assert.equal(absent[0]?.[0], "Reader checkpoint");
  assert.equal(absent[0]?.[1], "Unknown — execution not projected");
  assert.match(absent[1]?.[1] ?? '', /Not measured — measurement not published by the service/);

  const runtime = positionRows(pipelineFixture({
    fleetRuntime: { checkpoint: { receiver: 'TRNJRN3776', sequence: 41 } } as Pipeline['fleetRuntime'],
  }));
  assert.match(runtime[0]?.[1] ?? '', /TRNJRN3776\s·\s41 \(runtime\)/);

  const served = positionRows(pipelineFixture({ position: positionFixture() }));
  assert.equal(served.length, 4);
  assert.equal(served[1]?.[0], "Journal end");
  assert.match(served[1]?.[1] ?? '', /TRNJRN3776\s·\s1,337/);
  assert.equal(served[2]?.[1], '40');
});

/* ------------------------------------------------------------------ */
/* Tampon apply — la seule preuve verte de livraison                    */
/* ------------------------------------------------------------------ */

test('apply stamp is uninstrumented when the extension is absent', () => {
  const stamp = destinationApplyStamp(pipelineFixture());
  assert.equal(stamp.measured, false);
  assert.equal(stamp.label, "date not served");
  assert.equal(stamp.reason, "measurement not published by the service");
  assert.equal(stamp.at, null);
});

test('apply stamp cites the declared reason when the block is detached', () => {
  const stamp = destinationApplyStamp(pipelineFixture({
    destination: null,
    destinationReason: 'destination_not_attached',
  }));
  assert.equal(stamp.measured, false);
  assert.equal(stamp.label, "date not served");
  assert.equal(stamp.reason, "destination not linked to the observation");
});

test('apply stamp never turns a served block without observation into an arrival', () => {
  const stamp = destinationApplyStamp(pipelineFixture({
    destination: destinationFixture({ observedAt: null }),
  }));
  assert.equal(stamp.measured, false);
  assert.equal(stamp.label, "date not observed");
  assert.match(stamp.reason ?? '', /without an application date/);
});

test('apply stamp renders the measured apply clock — the only green proof', () => {
  const stamp = destinationApplyStamp(pipelineFixture({ destination: destinationFixture() }));
  assert.equal(stamp.measured, true);
  assert.equal(stamp.label, "delivered 09:42:13");
  assert.equal(stamp.at, '2026-09-13T09:42:13Z');
});

test('destination stamp is green only on a measured apply within current evidence', () => {
  const pipeline = pipelineFixture({
    destination: destinationFixture(),
    fleetPlan: { destinationNamespace: 'ACME_RAW.DEV' } as Pipeline['fleetPlan'],
  });
  const current = destinationStamp(pipeline, true);
  assert.equal(current.value, "delivered 09:42:13");
  assert.equal(current.tone, 'positive');
  assert.match(current.hint ?? '', /ACME_RAW\.DEV/);
  assert.match(current.hint ?? '', /destination observation/);

  const retained = destinationStamp(pipeline, false);
  assert.equal(retained.tone, 'muted');
});

test('destination stamp cites the window counter without calling it an arrival', () => {
  const pipeline = pipelineFixture({
    counters: { events_in_target: 120 },
    fleetPlan: { destinationNamespace: 'ACME_RAW.DEV' } as Pipeline['fleetPlan'],
  });
  const stamp = destinationStamp(pipeline, true);
  assert.equal(stamp.value, `120 events observed`);
  assert.match(stamp.hint ?? '', /date not served/);
  assert.match(stamp.hint ?? '', /not a reconciliation/);
  assert.equal(stamp.tone, 'active');
});

test('destination stamp stays uninstrumented without apply, counter or namespace', () => {
  const stamp = destinationStamp(pipelineFixture(), true);
  assert.equal(stamp.value, "Destination · undated");
  assert.equal(stamp.hint, "measurement not published by the service");
  assert.equal(stamp.tone, 'muted');
});

test('destination reason label maps known reasons and passes unknowns through', () => {
  assert.equal(destinationReasonLabel(undefined), "reason not configured");
  assert.equal(destinationReasonLabel(null), "reason not configured");
  assert.equal(destinationReasonLabel('destination_not_attached'), "destination not linked to the observation");
  assert.equal(destinationReasonLabel('reason_inconnue'), 'reason_inconnue');
});

/* ------------------------------------------------------------------ */
/* Chaîne des cinq étapes — apply uniquement sur destination            */
/* ------------------------------------------------------------------ */

test('stage chain carries five steps and apply lives only on destination', () => {
  const chain = stageChain(pipelineFixture({ destination: destinationFixture() }), now, true);
  assert.equal(chain.length, 5);
  for (const step of chain) {
    if (step.id === 'destination') {
      assert.equal(step.apply, "delivered 09:42:13");
      assert.equal(step.tone, 'positive');
    } else {
      assert.equal(step.apply, null);
      assert.equal(step.tone, 'active');
    }
  }
  const retained = stageChain(pipelineFixture({ destination: destinationFixture() }), now, false);
  assert.equal(retained[4]?.tone, 'muted');
  assert.equal(retained[0]?.tone, 'muted');
  // Relevé figé : une étape saine est observée, pas disponible à l'instant présent.
  for (const step of retained) {
    assert.equal(step.statusLabel, "Observed", `étape ${step.id}`);
  }
});

test('stage chain marks incidents, degradations and never-observed stages honestly', () => {
  const pipeline = pipelineFixture({
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'ok' },
      { id: 'capture', status: 'incident', observedAt, headline: "Capture interrupted", detail: 'timeout' },
      { id: 'raw', status: 'degraded', observedAt, headline: 'Raw partiel', detail: 'trous' },
      { id: 'load', status: 'unknown', observedAt: null, headline: "Loading non observed", detail: 'absent' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: "Destination not observed", detail: 'absent' },
    ],
  });
  const chain = stageChain(pipeline, now, true);
  assert.equal(chain[1]?.tone, 'attention');
  assert.equal(chain[2]?.tone, 'attention');
  assert.equal(chain[3]?.tone, 'muted');
  assert.equal(chain[3]?.ageLabel, "never observed");
  assert.equal(chain[3]?.statusLabel, "Unavailable");
  assert.equal(chain[4]?.apply, null);
});

/* ------------------------------------------------------------------ */
/* Verdict lag projeté — déclaré par le worker, jamais déduit           */
/* ------------------------------------------------------------------ */

test('lag verdict is uninstrumented when neither verdict nor reason is served', () => {
  const verdict = projectedLagVerdict(pipelineFixture());
  assert.equal(verdict.instrumented, false);
  assert.equal(verdict.label, NOT_INSTRUMENTED);
  assert.equal(verdict.reason, "measurement not published by the service");
});

test('lag verdict passes the declared verdict through the public allowlist', () => {
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'STABLE' })).label, 'Stable');
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'BOUNDED' })).label, "Bounded");
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'CATCHING_UP' })).label, "Catching up");
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'DIVERGING' })).label, 'Divergence');
  // verdict hors allowlist : pass-through brut, jamais reformulé
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'WILD' })).label, 'WILD');
});

test('lag verdict without a verdict cites the declared reason', () => {
  const declared = projectedLagVerdict(pipelineFixture({
    lagVerdict: null,
    lagVerdictReason: 'verdict_not_declared',
  }));
  assert.equal(declared.instrumented, true);
  assert.equal(declared.label, "Undetermined");
  assert.equal(declared.reason, "verdict not reported by the worker");

  const raw = projectedLagVerdict(pipelineFixture({ lagVerdict: null, lagVerdictReason: 'weird' }));
  assert.equal(raw.reason, 'weird');

  const silent = projectedLagVerdict(pipelineFixture({ lagVerdict: null }));
  assert.equal(silent.reason, "reason not configured");
});

/* ------------------------------------------------------------------ */
/* Exécution projetée — états mappés, jamais d'état inventé             */
/* ------------------------------------------------------------------ */

test('run state labels map the contract and pass unknowns through', () => {
  assert.equal(runStateLabel(null), "Unknown — state not published");
  assert.equal(runStateLabel('RUNNING'), "In progress");
  assert.equal(runStateLabel('PAUSED_SOURCE'), "Source pause");
  assert.equal(runStateLabel('STOPPED_BUDGET'), "Budget stop");
  assert.equal(runStateLabel('STOPPED_PROOF_CHAIN'), "Stopped — windows closed");
  assert.equal(runStateLabel('STOPPED_FAIL_CLOSED'), "Stopped safely");
  assert.equal(runStateLabel('STOPPED_AUTH_BLOCKED'), "Authentication blocked");
  assert.equal(runStateLabel('FUTURE_STATE'), 'FUTURE_STATE');
});

test('run detail rows are uninstrumented when the block is absent', () => {
  const rows = runDetailRows(pipelineFixture());
  assert.equal(rows.length, 6);
  assert.match(rows[0]?.[1] ?? '', /Not measured — measurement not published by the service/);
  assert.ok(rows.slice(1).every(([, value]) => /^Not measured/.test(value)));
});

test('run detail rows project measured members and cite absent ones', () => {
  const rows = runDetailRows(pipelineFixture({ run: runFixture() }));
  const map = new Map(rows);
  assert.equal(map.get("Run state"), "In progress");
  assert.match(map.get("Start") ?? '', /2026-09-13/);
  assert.equal(map.get("Measured duration"), `1${THIN}h`);
  assert.equal(map.get("Stop reason"), "None configured");
  assert.match(map.get("Last diagnostic") ?? '', /type budget/);
  assert.match(map.get("Last diagnostic") ?? '', /quota atteint/);
  assert.match(map.get("Source pause") ?? '', /code journal_wrap/);

  const minimal = runDetailRows(pipelineFixture({
    run: runFixture({ startedAt: null, elapsedSeconds: null, diagnostic: null, sourcePause: null }),
  }));
  const minimalMap = new Map(minimal);
  assert.equal(minimalMap.get("Start"), "Unknown — not measured");
  assert.equal(minimalMap.get("Measured duration"), "Unknown — not measured");
  assert.equal(minimalMap.get("Last diagnostic"), "No diagnostic published");
  assert.equal(minimalMap.get("Source pause"), "No source pause configured");
});

/* ------------------------------------------------------------------ */
/* Identité du flux et couverture des tables                            */
/* ------------------------------------------------------------------ */

test('flux identity rows are uninstrumented when the block is absent', () => {
  const rows = fluxIdentityRows(pipelineFixture());
  assert.equal(rows.length, 6);
  assert.match(rows[0]?.[1] ?? '', /Not measured — measurement not published by the service/);
});

test('flux identity rows join served members and cite unpublished ones', () => {
  const rows = fluxIdentityRows(pipelineFixture({ flux: fluxFixture() }));
  const map = new Map(rows);
  assert.equal(map.get("Flow"), 'CNTR · dev-cntr');
  assert.equal(map.get('Journal'), 'ACME_LIB/QJRN');
  assert.equal(map.get("Configured objects"), "CNTR, ACME.CUSTOMERS");
  assert.equal(map.get("Reader path"), 'journal');
  assert.equal(map.get("Target"), 'snowflake');
  assert.equal(map.get('Job'), 'CDCREADER1');

  const sparse = fluxIdentityRows(pipelineFixture({
    flux: fluxFixture({ journalLibrary: null, objects: null, job: null }),
  }));
  const sparseMap = new Map(sparse);
  assert.equal(sparseMap.get('Journal'), 'QJRN');
  assert.equal(sparseMap.get("Configured objects"), "Unknown — not published");
  assert.equal(sparseMap.get('Job'), "Unknown — not published");

  const empty = fluxIdentityRows(pipelineFixture({ flux: fluxFixture({ objects: [] }) }));
  assert.equal(new Map(empty).get("Configured objects"), "No objects configured");
});

test('flux object coverage distinguishes absent, unpublished, covered and outside tables', () => {
  assert.match(fluxObjectCoverage(undefined, 'CNTR'), /Not measured — connection identity absent/);
  assert.match(fluxObjectCoverage(null, 'CNTR'), /Not measured — connection identity absent/);
  assert.match(fluxObjectCoverage(fluxFixture({ objects: null }), 'CNTR'), /connection content not published/);

  const flux = fluxFixture();
  assert.equal(fluxObjectCoverage(flux, 'CNTR'), "Configured in the connection");
  assert.equal(fluxObjectCoverage(flux, 'cntr'), "Configured in the connection");
  assert.equal(fluxObjectCoverage(flux, "CUSTOMERS"), "Configured in the connection"); // ACME.CLIENTS
  assert.equal(fluxObjectCoverage(flux, 'DEVIS'), "Outside the configured scope");
});

/* ------------------------------------------------------------------ */
/* Destination — registre L3, comptes cités comme mesurés               */
/* ------------------------------------------------------------------ */

test('destination rows are uninstrumented when the extension is absent', () => {
  const rows = destinationRows(pipelineFixture());
  const map = new Map(rows);
  assert.match(map.get("Destination block") ?? '', /Not measured — measurement not published by the service/);
  assert.ok(rows.slice(1).every(([, value]) => /^Not measured/.test(value)));
});

test('destination rows cite the detach reason when the block is null', () => {
  const rows = destinationRows(pipelineFixture({
    destination: null,
    destinationReason: 'destination_not_attached',
  }));
  const map = new Map(rows);
  assert.match(map.get("Destination block") ?? '', /Not linked — destination not linked to the observation/);
});

test('destination rows project measured members and cite unmeasured ones', () => {
  const rows = destinationRows(pipelineFixture({ destination: destinationFixture() }));
  const map = new Map(rows);
  assert.match(map.get("Destination observation") ?? '', /2026-09-13/);
  assert.equal(map.get("Provenance"), 'snowflake');
  assert.equal(map.get("Database"), 'ACME_RAW');
  assert.equal(map.get("Schema"), 'DEV');
  assert.equal(map.get('Run tag'), 'run-41');
  assert.match(map.get("Loader checkpoint") ?? '', /snowpipe\s·\s900/);
  assert.match(map.get("Application checkpoint") ?? '', /snowpipe\s·\s901/);
  assert.equal(map.get("Source events"), '120');
  assert.equal(map.get("Raw rows"), '120');
  assert.equal(map.get("Canonical rows"), '118');
  assert.equal(map.get("Duplicates"), '2');

  const sparse = destinationRows(pipelineFixture({
    destination: destinationFixture({
      observedAt: null,
      applyCheckpoint: null,
      canonicalRows: null,
      duplicates: null,
      runTag: null,
    }),
  }));
  const sparseMap = new Map(sparse);
  assert.equal(sparseMap.get("Destination observation"), "Unknown — not measured");
  assert.equal(sparseMap.get("Application checkpoint"), "Unknown — not measured");
  assert.equal(sparseMap.get("Canonical rows"), "Unknown — not measured");
  assert.equal(sparseMap.get('Run tag'), "Unknown — not published");
});

test('destination detail sections expose the apply stamp alongside the register', () => {
  const sections = destinationDetailSections(pipelineFixture({ destination: destinationFixture() }));
  assert.equal(sections.applyStamp.measured, true);
  assert.equal(sections.applyStamp.label, "delivered 09:42:13");
  assert.ok(sections.rows.length > 0);
});
