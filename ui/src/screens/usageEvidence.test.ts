import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { Pipeline } from '../domain/controlPlane.ts';

const observedAt = '2026-08-28T08:33:56.470000+00:00';
let vite: ViteDevServer;
let Usage: ComponentType<any>;

/** Les formateurs produisent des espaces fines insécables ; on les normalise
 *  avant de comparer du texte, comme le fait operator.test.ts. */
function plain(value: string): string {
  return value.replace(/[   ]/g, ' ');
}

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  Usage = (await vite.ssrLoadModule('/src/screens/Usage.tsx')).Usage;
});

after(async () => { await vite.close(); });

test('la Consommation ouvre sur l’en-tête produit et le bouton d’actualisation', () => {
  const markup = render([pipelineFixture('alpha', { events_published: 1_793 })]);

  assert.match(markup, /<h1 class="board__title">Usage<\/h1>/);
  assert.match(markup, /Resources consumed by your copies\./);
  assert.match(markup, /<button type="button" class="board__refresh">Refresh<\/button>/);
  assert.match(markup, /<section class="consumption" key="alpha"|<section class="consumption"/);
  assert.match(markup, /<h2 class="consumption__title" id="conso-alpha">alpha<\/h2>/);
});

test('loading et failed restent une lecture en cours, jamais une absence confirmée', () => {
  for (const state of [
    { status: 'loading', connection: 'connecting' } as const,
    { status: 'failed', connection: 'offline', message: 'offline' } as const,
  ]) {
    const markup = renderToStaticMarkup(createElement(Usage, { state, overview: null, pipelines: [], onRefresh() {} }));
    assert.match(markup, /<h2>Reading<\/h2>/);
    assert.match(markup, /Quadringent is querying the service\./);
    assert.doesNotMatch(markup, /No connections/);
    assert.doesNotMatch(markup, /class="consumption"/);
  }
});

test('un relevé confirmé sans liaison dit l’absence, jamais une lecture en cours', () => {
  const state: ControlPlaneState = { status: 'ready', connection: 'live', overview: overviewFixture([]), pipelines: [], lastSuccessAt: new Date(observedAt) };
  const markup = renderToStaticMarkup(createElement(Usage, { state, overview: overviewFixture([]), pipelines: [], onRefresh() {} }));

  assert.match(markup, /<h2>No connections<\/h2>/);
  assert.match(markup, /Usage will appear after a connection has run\./);
  assert.doesNotMatch(markup, /Reading/);
});

test('plusieurs liaisons produisent plusieurs sections, chacune avec ses propres mesures', () => {
  const markup = render([
    pipelineFixture('alpha', { events_published: 1_793 }),
    pipelineFixture('bravo', { events_published: 42 }),
  ]);

  assert.equal((markup.match(/<section class="consumption"/g) ?? []).length, 2);
  assert.match(markup, /id="conso-alpha"/);
  assert.match(markup, /id="conso-bravo"/);
});

test('une mesure absente se dit « Non mesuré », jamais zéro — une mesure à zéro reste zéro', () => {
  // run_duration_s = 0 est une vraie mesure ; payload_bytes_published absent
  // ne doit jamais devenir un zéro fabriqué.
  const markup = render([pipelineFixture('alpha', { run_duration_s: 0 })]);

  assert.match(markup, /<dt>Processing time<\/dt><dd data-numeric="">0\s*s<\/dd>/);
  assert.match(markup, /<dt>Transferred volume<\/dt><dd class="consumption__absent" data-numeric="">Not measured<\/dd>/);
  assert.doesNotMatch(markup, /<dd data-numeric="">0<\/dd>/);
});

test('aucune mesure publiée dit l’absence plutôt que d’afficher un tableau vide', () => {
  const markup = render([pipelineFixture('alpha', {}, null)]);

  assert.match(markup, /No usage measurement has been published for this connection yet\./);
  assert.match(markup, /Not measured/);
  assert.match(markup, /Price not configured/);
});

test('la limite du produit est dite pour chaque liaison, y compris l’absence de crédits Snowflake', () => {
  const withSnowflake = render([pipelineFixture('alpha', { events_published: 1 }, { kind: 'snowflake' })]);
  assert.match(withSnowflake, /What Quadringent does not measure/);
  assert.match(withSnowflake, /Consumed Snowflake credits have not been measured yet\./);
  assert.match(withSnowflake, /No rate configured: credits are shown without currency conversion\./);

  const withoutDestination = render([pipelineFixture('alpha', { events_published: 1 }, null)]);
  assert.doesNotMatch(withoutDestination, /Snowflake credits consommés ne sont pas relevés/);
  assert.match(withoutDestination, /No rate configured/);
});

function render(pipelines: readonly Pipeline[]): string {
  const overview = overviewFixture(pipelines);
  const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines, lastSuccessAt: new Date(observedAt) };
  return renderToStaticMarkup(createElement(Usage, { state, overview, pipelines, onRefresh() {} }));
}

function overviewFixture(pipelines: readonly Pipeline[]) {
  return {
    revision: 3,
    generatedAt: observedAt,
    scope: { kind: 'single' as const, environments: ['dev'] },
    pipelines,
    sources: pipelines.map((pipeline) => ({
      id: `source-${pipeline.id}`,
      environment: pipeline.environment,
      evidenceKind: 'live' as const,
      status: 'available' as const,
      error: null,
    })),
  };
}

function pipelineFixture(
  id: string,
  counters: Pipeline['counters'],
  destination: Pipeline['destination'] | undefined = { kind: 'snowflake', database: 'DEV_RAW', schema: 'AS400_RD', stage: null, rawTable: null, canonicalTable: null, runTag: null, observedAt, loadCheckpoint: null, applyCheckpoint: null, sourceEvents: null, rawRows: null, canonicalRows: 12, duplicates: 0 },
): Pipeline {
  return {
    id,
    environment: 'dev',
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    summary: 'Toutes les étapes déclarées observées',
    observedAt,
    stages: [],
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters,
    incident: null,
    destination: destination ?? null,
  };
}
