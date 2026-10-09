import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { Incident, Overview, Pipeline, PipelineStatus } from '../domain/controlPlane.ts';

const observedAt = '2026-08-28T08:33:56.470000+00:00';
let vite: ViteDevServer;
let Incidents: ComponentType<any>;
const originalTimezone = process.env.TZ;

before(async () => {
  // Ce scénario vérifie 10:33 à Paris ; le poste ou la CI peut être en UTC.
  process.env.TZ = 'Europe/Paris';
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  Incidents = (await vite.ssrLoadModule('/src/screens/Incidents.tsx')).Incidents;
});

after(async () => {
  await vite.close();
  if (originalTimezone === undefined) delete process.env.TZ;
  else process.env.TZ = originalTimezone;
});

test('le Journal ouvre sur l’en-tête produit et le bouton d’actualisation', () => {
  const markup = render([pipelineFixture('alpha')]);
  assert.match(markup, /<h1 class="board__title">Journal<\/h1>/);
  assert.match(markup, /What happened on your connections\./);
  assert.match(markup, /<button type="button" class="board__refresh">Refresh<\/button>/);
});

test('loading et failed distinguent une lecture en cours d’un journal qui n’a pas pu être lu', () => {
  const loading = renderToStaticMarkup(createElement(Incidents, {
    state: { status: 'loading', connection: 'connecting' }, overview: null, pipelines: [], onRefresh() {},
  }));
  assert.match(loading, /<h2>Reading<\/h2>/);
  assert.match(loading, /Quadringent is querying the service\./);

  const failed = renderToStaticMarkup(createElement(Incidents, {
    state: { status: 'failed', connection: 'offline', message: 'offline' }, overview: null, pipelines: [], onRefresh() {},
  }));
  assert.match(failed, /<h2>Logs not read<\/h2>/);
  assert.match(failed, /The service did not respond\. No event is displayed\./);
  assert.doesNotMatch(failed, /Nothing to report/);
});

test('un relevé confirmé sans événement dit qu’il n’y a rien à signaler', () => {
  const markup = render([pipelineFixture('alpha')]);
  assert.match(markup, /<h2>Nothing to report<\/h2>/);
  assert.match(markup, /No events have been recorded on your connections\./);
  assert.doesNotMatch(markup, /Reading/);
});

test('les liaisons qui demandent une attention apparaissent en tête, les liaisons à jour en sont absentes', () => {
  const ok = pipelineFixture('alpha');
  const stopped = pipelineFixture('bravo', 'incident', { code: 'capture_stopped_fail_closed', type: 'capture_stopped' });
  const markup = render([ok, stopped]);

  assert.match(markup, /<section class="history-attention" aria-label="What needs your attention">/);
  assert.equal((markup.match(/class="history-attention__item"/g) ?? []).length, 1);
  assert.match(markup, /<span class="history-attention__name">bravo<\/span>/);
  assert.match(markup, /<span class="history-attention__state">Interrupted<\/span>/);
  assert.match(markup, /href="#\/pipeline\/bravo"/);
  assert.doesNotMatch(markup, /history-attention__name">alpha</);
});

test('un incident produit une ligne de journal datée, groupée par jour, avec son détail', () => {
  const markup = render([pipelineFixture('alpha', 'incident', { code: 'capture_stopped_fail_closed', type: 'capture_stopped' })]);

  assert.match(markup, /class="history"/);
  assert.match(markup, /class="history__day"/);
  assert.match(markup, /<h2 class="history__date">Friday, August 28<\/h2>/);
  assert.match(markup, /<li class="history__line history__line--attention">/);
  assert.match(markup, /<time class="history__time" dateTime="2026-08-28T08:33:56\.470000\+00:00">10:33<\/time>/);
  assert.match(markup, /<p class="history__label">Capture stopped safely<\/p>/);
  assert.match(markup, /<p class="history__detail">fail-closed stop/);
});

test('un arrêt planifié se lit comme un fait neutre, jamais un incident', () => {
  const markup = render([pipelineFixture('alpha', 'planned_stop')]);
  assert.match(markup, /<li class="history__line history__line--muted">/);
  assert.match(markup, /<p class="history__label">stop requested<\/p>/);
  assert.doesNotMatch(markup, /history__line--attention/);
});

test('une reprise en cours se lit comme un fait actif, distinct d’un incident', () => {
  const markup = render([pipelineFixture('alpha', 'recovering')]);
  assert.match(markup, /<li class="history__line history__line--active">/);
  assert.match(markup, /<p class="history__label">Recovery in progress<\/p>/);
});

test('le journal ne redonne jamais un ordre — le vocabulaire interne ne survit pas', () => {
  const markup = render([pipelineFixture('alpha', 'incident', { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked' })]);
  assert.doesNotMatch(markup, /relancer the reader/i);
  assert.doesNotMatch(markup, /measured|derived|observed · frozen/i);
});

test('les styles du journal existent et restent sans décor interdit', () => {
  const css = readFileSync(new URL('../styles/history.css', import.meta.url), 'utf8');
  assert.match(css, /\.history\s*\{/);
  assert.match(css, /\.history__line\s*\{/);
  assert.match(css, /\.history-attention\s*\{/);
  assert.doesNotMatch(css, /linear-gradient|radial-gradient|backdrop-filter|blur\(/);
});

function render(pipelines: readonly Pipeline[]): string {
  const overview = overviewFixture(pipelines);
  const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines, lastSuccessAt: new Date(observedAt) };
  return renderToStaticMarkup(createElement(Incidents, { state, overview, pipelines, onRefresh() {} }));
}

function overviewFixture(pipelines: readonly Pipeline[]): Overview {
  return {
    revision: 1,
    generatedAt: observedAt,
    scope: { kind: 'single', environments: ['dev'] },
    pipelines,
    sources: pipelines.map((pipeline) => ({
      id: `source-${pipeline.id}`,
      environment: pipeline.environment,
      evidenceKind: 'live',
      status: 'available',
      error: null,
    })),
  };
}

function pipelineFixture(
  id: string,
  status: PipelineStatus = 'healthy',
  incident: Incident | null = null,
): Pipeline {
  return {
    id,
    environment: 'dev',
    status,
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    summary: "Delivery observed dans son périmètre",
    observedAt,
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'Signal présent' },
      { id: 'capture', status: status === 'planned_stop' ? 'planned_stop' : 'healthy', observedAt, headline: 'Capture observée', detail: 'Signal présent' },
      { id: 'raw', status: 'healthy', observedAt, headline: "Raw observed", detail: 'Signal présent' },
      { id: 'load', status: 'healthy', observedAt, headline: 'Load', detail: 'Signal' },
      { id: 'destination', status: 'healthy', observedAt, headline: 'Destination', detail: 'Signal' },
    ],
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident,
  };
}
