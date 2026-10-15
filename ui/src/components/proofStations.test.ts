import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import type { Pipeline } from '../domain/controlPlane.ts';
import { resolvePipelineProofFocus } from '../domain/proofFocus.ts';
import {
  afterBoundaryCopy,
  captureBoardFact,
  captureStateBoardFact,
  captureVolumeParts,
  captureVolumesBoardFact,
  destinationLimitFact,
  failClosedFrontier,
  lagBoardFact,
  measureSubordination,
  observedCounter,
  proofChamber,
  proofStageOrder,
  proofStations,
  windowsBoardFact,
} from './proofStations.ts';

const observedAt = '2026-08-28T08:33:56.470000+00:00';

test('proof stations always expose the five-stage boundary with destination in the same instrument', () => {
  const pipeline = pipelineFixture();
  const focus = resolveFocus(pipeline);
  const stations = proofStations(focus, pipeline);

  assert.deepEqual(stations.map((station) => station.id), [...proofStageOrder]);
  assert.deepEqual(stations.map((station) => station.label), ['Source', "Reader", "Reception area", "Initial copy", 'Destination']);
  assert.equal(stations.filter((station) => station.isFocus).length, 1);
  assert.equal(stations.at(-1)?.isDestination, true);
  assert.equal(stations.at(-1)?.id, 'destination');
  assert.notEqual(stations.at(-1)?.kind, 'established');
  assert.equal(stations.every((station) => station.label.length > 0), true);
});

test('the first break dominates while stations after the boundary stay explicit and visible', () => {
  const pipeline = pipelineFixture();
  const focus = resolveFocus(pipeline);
  const stations = proofStations(focus, pipeline);
  const breakStation = stations.find((station) => station.isFocus);

  assert.equal(focus.firstBreak?.stage, 'load');
  assert.equal(breakStation?.id, 'load');
  assert.equal(breakStation?.kind, 'break');
  assert.equal(stations[0]?.kind, 'established');
  assert.equal(stations[1]?.kind, 'established');
  assert.equal(stations[2]?.kind, 'established');
  assert.equal(stations[4]?.isFocus, false);
  assert.equal(stations[4]?.afterBoundary, true);
  assert.match(stations[4]?.summary ?? '', /Unconfirmed/);
  assert.match(stations[4]?.summary ?? '', /unconfirmed/i);
  assert.match(stations[4]?.summary ?? '', new RegExp(afterBoundaryCopy));

  const chamber = proofChamber(focus, pipeline);
  assert.deepEqual(chamber.upstream.map((station) => station.id), ['source', 'capture', 'raw']);
  assert.equal(chamber.boundary?.id, 'load');
  assert.equal(chamber.arrival?.id, 'destination');
  assert.equal(chamber.arrival?.isDestination, true);
});

test('stations after an earlier rupture stay non confirmable and never hide a case', () => {
  const pipeline = pipelineFixture({
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'Checkpoint et tail présents' },
      { id: 'capture', status: 'unknown', observedAt: null, headline: 'Capture stopped by budget', detail: "None checkpoint" },
      { id: 'raw', status: 'unknown', observedAt: null, headline: "Raw non observed", detail: 'Publication absente' },
      { id: 'load', status: 'unknown', observedAt: null, headline: "Loading non observed", detail: 'Aucune preuve de load' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: "Destination not observed", detail: 'Aucune preuve d’application' },
    ],
  });
  const stations = proofStations(resolveFocus(pipeline), pipeline);

  assert.equal(stations[1]?.isFocus, true);
  assert.deepEqual(stations.filter((station) => station.afterBoundary).map((station) => station.id), ['raw', 'load', 'destination']);
  for (const station of stations.filter((item) => item.afterBoundary)) {
    assert.match(station.summary, new RegExp(afterBoundaryCopy));
    assert.match(station.summary, /unconfirm/i);
  }
});

test('capture, raw, load and destination reuse existing facts and never invent health', () => {
  const pipeline = pipelineFixture({
    evidenceKind: 'historical',
    lagSeconds: 12,
    counters: {
      events_published: 1793,
      payload_bytes_published: 622091,
      windows_published: 225,
      events_in_target: null,
    },
  });
  const stations = proofStations(resolveFocus(pipeline), pipeline);
  const capture = stations.find((station) => station.id === 'capture');
  const raw = stations.find((station) => station.id === 'raw');
  const load = stations.find((station) => station.id === 'load');
  const destination = stations.find((station) => station.id === 'destination');

  assert.match(capture?.summary ?? '', /Capture active/);
  assert.match(capture?.summary ?? '', /lag of 12/);
  assert.doesNotMatch(capture?.summary ?? '', /1,793|622,091|225 windows|sortie capture, pas Snowflake/);
  assert.match(captureVolumesBoardFact(pipeline), /1,793 events/);
  assert.match(captureVolumesBoardFact(pipeline), /622,091 bytes/);
  assert.match(captureVolumesBoardFact(pipeline), /225 batches/);
  assert.match(captureVolumesBoardFact(pipeline), /read at the source, not at the destination/);
  assert.doesNotMatch(raw?.summary ?? '', /1,793|622,091|225 windows|sortie capture, pas Snowflake/);
  assert.match(raw?.summary ?? '', /Raw published/);
  assert.match(load?.summary ?? '', /Unconfirmed/);
  assert.match(load?.summary ?? '', /earlier/i);
  assert.doesNotMatch(load?.summary ?? '', /1,793|622,091|225 windows/);
  assert.match(destination?.summary ?? '', /Unconfirmed/);
  assert.doesNotMatch(destination?.summary ?? '', /1,793|622,091|225 windows|santé|healthy/i);
  assert.equal(observedCounter(pipeline, 'events_in_target'), null);
  assert.doesNotMatch(`${capture?.summary}${raw?.summary}${load?.summary}${destination?.summary}`, /\bhealth\b/i);
});

test('historical lag 0 is subordinated to a budget-stopped capture and never reads as health', () => {
  const pipeline = pipelineFixture({
    evidenceKind: 'historical',
    lagSeconds: 0,
    lagSequences: 0,
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'Checkpoint et tail présents' },
      { id: 'capture', status: 'unknown', observedAt: null, headline: 'Capture stopped by budget', detail: "None checkpoint" },
      { id: 'raw', status: 'unknown', observedAt: null, headline: "Raw non observed", detail: 'Publication absente' },
      { id: 'load', status: 'unknown', observedAt: null, headline: "Loading non observed", detail: 'Aucune preuve de load' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: "Destination not observed", detail: 'Aucune preuve d’application' },
    ],
  });
  const fact = lagBoardFact(pipeline);
  assert.match(fact, /lag of 0 records/);
  assert.match(fact, /subordinate to the reader stopped by budget/);
  assert.match(fact, /not a current health signal/);
  assert.doesNotMatch(fact, /\blive health\b|healthy/i);
  assert.equal(measureSubordination(pipeline), "Reader stopped by budget, not a current health signal");
  const capture = proofStations(resolveFocus(pipeline), pipeline).find((station) => station.id === 'capture');
  assert.match(capture?.summary ?? '', /Capture stopped by budget/);
  assert.match(capture?.summary ?? '', /subordinate to the reader stopped by budget/);
  assert.match(capture?.summary ?? '', /not a current health signal/);
  assert.doesNotMatch(capture?.summary ?? '', /1,793|622,091|225 windows|sortie capture, pas Snowflake/);
});

test('unknown counters stay unestablished instead of becoming zero', () => {
  const pipeline = pipelineFixture({
    lagSeconds: null,
    lagSequences: null,
    counters: { events_published: null, payload_bytes_published: null, windows_published: null, events_in_target: null },
  });
  const stations = proofStations(resolveFocus(pipeline), pipeline);
  const capture = stations.find((station) => station.id === 'capture')?.summary ?? '';
  const raw = stations.find((station) => station.id === 'raw')?.summary ?? '';
  const load = stations.find((station) => station.id === 'load')?.summary ?? '';
  const destination = stations.find((station) => station.id === 'destination')?.summary ?? '';

  assert.doesNotMatch(capture, /read events not established|bytes not established|batches not established|1,793|622,091/);
  assert.match(capture, /lag not established/);
  assert.doesNotMatch(raw, /read events not established|bytes not established|batches not established|1,793|622,091/);
  assert.doesNotMatch(load, /read events not established|1,793|622,091/);
  assert.doesNotMatch(raw, /(?:^|[^\d])0 events|(?:^|[^\d])0 bytes|(?:^|[^\d])0 lots/);
  assert.match(destination, /Unconfirmed/);
  assert.doesNotMatch(destination, /0 events|1,793|622,091/);
  assert.match(captureBoardFact(pipeline), /lag not established/);
  assert.equal(windowsBoardFact(pipeline), "Batches not established");
  assert.equal(captureStateBoardFact(pipeline), 'Capture active');
  assert.equal(lagBoardFact(pipeline), "lag not established");
  assert.deepEqual(captureVolumeParts(pipeline), {
    events: "read events not established",
    bytes: "bytes not established",
    windows: "batches not established",
    qualification: "read at the source, not at the destination",
  });
  assert.match(captureVolumesBoardFact(pipeline), /read events not established/);
  assert.match(destinationLimitFact(resolveFocus(pipeline), pipeline), /Measurement boundary/);
  assert.equal(captureStateBoardFact(null), "Reader state not established");
  assert.equal(lagBoardFact(null), "lag not established");
  assert.match(captureVolumesBoardFact(null), /read at the source, not at the destination/);
  assert.equal(destinationLimitFact(null, null), "Destination unconfirmed. Measurement boundary.");
});

test('capture volumes stay on Capture and are forbidden on Raw, Load and Destination', () => {
  const pipeline = pipelineFixture({
    evidenceKind: 'historical',
    counters: {
      events_published: 1793,
      payload_bytes_published: 622091,
      windows_published: 225,
      events_in_target: null,
    },
  });
  const stations = proofStations(resolveFocus(pipeline), pipeline);
  const byId = Object.fromEntries(stations.map((station) => [station.id, station.summary]));

  assert.match(byId.capture ?? '', /Capture active/);
  assert.doesNotMatch(byId.capture ?? '', /1,793 events/);
  assert.doesNotMatch(byId.capture ?? '', /622,091 bytes/);
  assert.doesNotMatch(byId.capture ?? '', /225 windows/);
  assert.doesNotMatch(byId.capture ?? '', /capture output, not Snowflake/);
  assert.match(captureVolumesBoardFact(pipeline), /1,793 events/);
  assert.match(captureVolumesBoardFact(pipeline), /read at the source, not at the destination/);
  for (const id of ['raw', 'load', 'destination'] as const) {
    assert.doesNotMatch(byId[id] ?? '', /1,793/);
    assert.doesNotMatch(byId[id] ?? '', /622,091/);
    assert.doesNotMatch(byId[id] ?? '', /225 windows/);
    assert.doesNotMatch(byId[id] ?? '', /capture output, not Snowflake/);
  }
});

test('destination may report events_in_target only when that count exists', () => {
  const pipeline = pipelineFixture({ counters: { events_in_target: 1064, events_published: 1793 } });
  const destination = proofStations(resolveFocus(pipeline), pipeline).at(-1);

  assert.match(destination?.summary ?? '', /1,064 events arrived/);
  assert.doesNotMatch(destination?.summary ?? '', /1,793/);
  assert.match(destination?.summary ?? '', /Unconfirmed/);
});

test('the shared chain stylesheet is a five-case bordereau, not a 248px poster', () => {
  const css = readFileSync(new URL('../styles/proof-chain.css', import.meta.url), 'utf8');
  assert.doesNotMatch(css, /min-height:\s*248px/);
  assert.doesNotMatch(css, /repeat\(5,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(css, /\.proof-chain__stations\s*\{[^}]*display:\s*grid/);
  assert.doesNotMatch(css, /border-left|border-inline-start/, 'no coloured vertical rail on a station');
  assert.match(css, /\.proof-chain__stations\[data-focus-stage='load'\]\s*\{[^}]*minmax\(0,\s*1\.28fr\)/);
  assert.match(css, /\.proof-chain__index\s*\{/);
});

test('the waybill is one continuous five-stage rail, not five equal cards', () => {
  const css = readFileSync(new URL('../styles/proof-chain.css', import.meta.url), 'utf8');
  const source = readFileSync(new URL('./ProofChain.tsx', import.meta.url), 'utf8');

  assert.match(source, /data-instrument="waybill"/);
  assert.match(source, /proof-chain__link/);
  assert.doesNotMatch(source, /proof-chain__masthead/);
  assert.match(css, /\.proof-chain__station\s*\{[^}]*border-top:\s*2px solid/);
  assert.match(css, /\.proof-chain__station--focus\s*\{[^}]*background:\s*transparent/);
  assert.doesNotMatch(css, /\.proof-chain__station--focus\s*\{[^}]*background:\s*var\(--surface-panel\)/);
  assert.doesNotMatch(css, /\.proof-chain__station--destination:not\([^)]+\)\s*\{[^}]*border-left:\s*1px solid/);
  assert.match(css, /\.proof-chain__link/);
  assert.match(css, /\.proof-waybill-index__station\s*\{[^}]*background:\s*transparent/);
  assert.doesNotMatch(css, /\.proof-waybill-index__station\.is-focus\s*\{[^}]*border-left/);
  assert.doesNotMatch(css, /border-left-color/);
  assert.match(css, /\.proof-chain--historical \.proof-chain__name,[\s\S]*?color:\s*var\(--text-muted\)/);
});

test('fail-closed frontier is Source when no more precise proof exists', () => {
  assert.equal(failClosedFrontier(null), 'source');
  assert.equal(failClosedFrontier(undefined), 'source');
  assert.equal(failClosedFrontier('load'), 'load');
  assert.equal(failClosedFrontier('destination'), 'destination');
});

test('the last commercial layer keeps five station columns at 1440 and never restores 3+2', () => {
  const commercial = readFileSync(new URL('../styles/commercial.css', import.meta.url), 'utf8');
  const chain = readFileSync(new URL('../styles/proof-chain.css', import.meta.url), 'utf8');
  const proofChainTsx = readFileSync(new URL('./ProofChain.tsx', import.meta.url), 'utf8');
  const main = readFileSync(new URL('../main.tsx', import.meta.url), 'utf8');
  const desktopCommercial = commercial.slice(0, commercial.indexOf('@media (max-width: 760px)'));
  const fiveTracks = /^(?:minmax\([^)]+\)\s+){4}minmax\([^)]+\)$/;
  const cssImports = [...main.matchAll(/import '\.\/styles\/([^']+\.css)'/g)].map((match) => match[1]);

  assert.equal(cssImports.at(-1), 'commercial.css');
  assert.doesNotMatch(desktopCommercial, /1\.85fr/);
  assert.doesNotMatch(desktopCommercial, /0\.92fr/);
  assert.doesNotMatch(desktopCommercial, /minmax\(148px,\s*0\.72fr\)\s+minmax\(0,\s*1\.85fr\)\s+minmax\(176px,\s*0\.92fr\)/);
  assert.doesNotMatch(proofChainTsx, /data-focus-stage=\{proof\.focus\.stage \?\? 'none'\}/);
  assert.match(proofChainTsx, /data-focus-stage=\{failClosedFrontier\(proof\.focus\.stage\)\}/);

  const stationRules = [...desktopCommercial.matchAll(/\.proof-chain__stations[^{]*\{([^}]*)\}/g)];
  assert.ok(stationRules.length >= 1, 'commercial.css must keep an explicit stations rule at desktop');
  let columnRules = 0;
  for (const [, body] of stationRules) {
    const columns = body.match(/grid-template-columns:\s*([^;]+)/);
    if (!columns) continue;
    columnRules += 1;
    const tracks = columns[1].match(/minmax\(/g) ?? [];
    assert.equal(tracks.length, 5, `desktop stations must stay 5 columns, got ${columns[1].trim()}`);
    assert.match(columns[1].trim(), fiveTracks);
    assert.doesNotMatch(columns[1], /1\.85fr|0\.92fr/);
  }
  assert.ok(columnRules >= 6, `expected default + 5 focus-stage desktop rules, got ${columnRules}`);

  assert.match(desktopCommercial, /data-focus-stage='source'\][^{]*\{[^}]*minmax\(0,\s*1\.28fr\)\s+minmax\(0,\s*0\.90fr\)/);
  assert.match(chain, /\.proof-chain__stations\s*\{[^}]*grid-template-columns:\s*(?:minmax\([^)]+\)\s+){4}minmax\(/);
  assert.match(chain, /\.proof-chain__stations\[data-focus-stage='source'\]\s*\{[^}]*minmax\(0,\s*1\.28fr\)/);
  assert.doesNotMatch(chain, /repeat\(5,\s*minmax\(0,\s*1fr\)\)/);
  assert.doesNotMatch(desktopCommercial, /repeat\(5,\s*minmax\(0,\s*1fr\)\)/);

  for (const [, body] of stationRules) {
    const columns = body.match(/grid-template-columns:\s*([^;]+)/);
    if (!columns) continue;
    const tracks = [...columns[1].matchAll(/minmax\(0,\s*([0-9.]+)fr\)/g)].map((match) => Number(match[1]));
    assert.equal(tracks.length, 5, `expected 5 numeric tracks, got ${columns[1].trim()}`);
    const quietest = Math.min(...tracks);
    const widest = Math.max(...tracks);
    assert.ok(quietest >= 0.85, `quiet station too thin at 1440: ${columns[1].trim()}`);
    assert.ok(widest <= 1.45, `focus station too cavernous at 1440: ${columns[1].trim()}`);
    assert.ok(widest > quietest, `stations must stay asymmetric: ${columns[1].trim()}`);
    assert.ok(new Set(tracks.map((track) => track.toFixed(2))).size > 1, 'five equal cards are forbidden');
  }

  const mobile = commercial.slice(commercial.indexOf('@media (max-width: 760px)'));
  assert.match(mobile, /\.overview-view \.proof-chain__stations,[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\)/);
});

function resolveFocus(pipeline: Pipeline) {
  return resolvePipelineProofFocus(pipeline, {
    generatedAt: observedAt,
    sources: [{ id: 'dev-source', environment: 'dev', evidenceKind: pipeline.quality.evidenceKind, status: 'available', error: null }],
    cached: false,
  });
}

function pipelineFixture(overrides: {
  readonly evidenceKind?: Pipeline['quality']['evidenceKind'];
  readonly lagSeconds?: number | null;
  readonly lagSequences?: number | null;
  readonly counters?: Pipeline['counters'];
  readonly stages?: Pipeline['stages'];
} = {}): Pipeline {
  return {
    id: 'local-proof',
    environment: 'dev',
    status: 'degraded',
    quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: overrides.evidenceKind ?? 'simulation' },
    summary: "Simulation : livraison Snowflake non proven",
    observedAt,
    stages: overrides.stages ?? [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'Checkpoint et tail présents' },
      { id: 'capture', status: 'healthy', observedAt, headline: 'Capture active', detail: "Worker active" },
      { id: 'raw', status: 'healthy', observedAt, headline: 'Raw published', detail: 'Publication confirmée' },
      { id: 'load', status: 'unknown', observedAt: null, headline: "Loading non observed", detail: 'Aucune preuve de load' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: "Destination not observed", detail: 'Aucune preuve d’application' },
    ],
    lagSequences: overrides.lagSequences === undefined ? 0 : overrides.lagSequences,
    lagSeconds: overrides.lagSeconds === undefined ? null : overrides.lagSeconds,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: overrides.counters ?? { events_published: 1793, receiver_rotations: 0 },
    incident: null,
  };
}
