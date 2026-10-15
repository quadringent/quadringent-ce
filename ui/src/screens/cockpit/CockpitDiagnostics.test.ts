import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { DiagnosticExport, DiagnosticReport, DiagnosticResult } from '../../data/diagnosticsContract.ts';
import type { DiagnosticsExportState, DiagnosticsState } from '../../data/useDiagnostics.ts';

let vite: ViteDevServer;
let CockpitDiagnosticsView: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  CockpitDiagnosticsView = (await vite.ssrLoadModule('/src/screens/cockpit/CockpitDiagnostics.tsx')).CockpitDiagnosticsView;
});

after(async () => { await vite.close(); });

function result(overrides: Partial<DiagnosticResult> = {}): DiagnosticResult {
  return {
    code: 'delivery.snowflake',
    layer: 'delivery',
    status: 'failed',
    required: true,
    observedAt: '2026-10-12T18:59:30Z',
    provenance: 'control-plane:evidence',
    subject: { kind: 'pipeline', ref: 'pl_123', label: 'SALES.ORDERS' },
    since: '2026-10-12T18:40:00Z',
    summary: 'Delivery to Snowflake is stale.',
    nextAction: 'Open the table logs.',
    limits: 'Collected by the loader log; not a reconciliation.',
    evidence: { stage_state: 'stale', evidence_kind: 'live' },
    evidenceKind: 'live',
    ...overrides,
  };
}

function report(results: readonly DiagnosticResult[], overrides: Partial<DiagnosticReport> = {}): DiagnosticReport {
  return {
    schema: 'quadringent.diagnostic.v1',
    generatedAt: '2026-10-12T19:00:00Z',
    scope: 'control_plane',
    synthetic: false,
    software: { product: 'quadringent', version: '0.3.5', distribution: 'control-plane' },
    overall: { status: 'blocked', passed: 0, failed: 1, unknown: 0, skipped: 0, summary: '' },
    results,
    ...overrides,
  };
}

const IDLE: DiagnosticsExportState = { status: 'idle' };

function render(state: DiagnosticsState, extra: Record<string, unknown> = {}): string {
  return renderToStaticMarkup(createElement(CockpitDiagnosticsView, {
    pipelineId: null,
    state,
    onRefresh() {},
    exportState: IDLE,
    onPrepareExport() {},
    onDiscardExport() {},
    onDownloadExport() {},
    downloaded: false,
    ...extra,
  }));
}

const ready = (value: DiagnosticReport, pipelineId: string | null = null): DiagnosticsState => ({ status: 'ready', report: value, pipelineId });

test('loading reads as loading and renders no check', () => {
  const markup = render({ status: 'loading' });
  assert.match(markup, /Reading diagnostics/);
  assert.doesNotMatch(markup, /diagnostic-result/);
});

test('each failure explains what happened and what to do, and a 429 keeps its delay', () => {
  const failure = (kind: string, extra: Record<string, unknown> = {}) => render({
    status: 'failed',
    failure: { kind, title: `T-${kind}`, detail: `D-${kind}`, serviceMessage: null, retryAfterSeconds: null, ...extra },
  } as DiagnosticsState);
  for (const kind of ['session', 'forbidden', 'not_found', 'rate_limited', 'unavailable', 'invalid_response']) {
    const markup = failure(kind);
    assert.match(markup, new RegExp(`role="alert"`));
    assert.match(markup, new RegExp(`T-${kind}`));
    assert.match(markup, new RegExp(`D-${kind}`));
    assert.doesNotMatch(markup, /diagnostic-result/);
  }
  assert.match(failure('rate_limited', { retryAfterSeconds: 30 }), /Retry in 30 s\./);
  assert.doesNotMatch(failure('rate_limited'), /Retry in/);
});

test('the overall block never claims health and states that it covers only the listed checks', () => {
  const markup = render(ready(report([result()])));
  assert.match(markup, /Blocked: a required check failed\./);
  assert.match(markup, /not a health verdict/i);
  assert.doesNotMatch(markup, /healthy/i);
});

test('an established problem answers which, about what, since when, proved by what, next step and limits', () => {
  const markup = render(ready(report([result()])));
  const problems = markup.slice(markup.indexOf('Established problems'), markup.indexOf('Required checks without an answer'));
  assert.match(problems, /Delivery to Snowflake is stale\./);
  assert.match(problems, /Table: SALES\.ORDERS/);
  assert.match(problems, /href="#\/cockpit\/table\/pl_123"/);
  assert.match(problems, /2026-10-12 18:40:00 UTC/);
  assert.match(problems, /2026-10-12 18:59:30 UTC/);
  assert.match(problems, /control-plane:evidence/);
  assert.match(problems, /Stage state/);
  assert.match(problems, /Open the table logs\./);
  assert.match(problems, /Collected by the loader log; not a reconciliation\./);
});

test('required checks without an answer are listed apart from failures and are not called failures', () => {
  const markup = render(ready(report([
    result({ code: 'source.tls', layer: 'tls', status: 'unknown', summary: 'No TLS fingerprint was confirmed.', subject: { kind: 'source', ref: 'src_1', label: 'Sales' }, evidence: {}, evidenceKind: null }),
  ], { overall: { status: 'incomplete', passed: 0, failed: 0, unknown: 1, skipped: 0, summary: '' } })));
  const gaps = markup.slice(markup.indexOf('Required checks without an answer'), markup.indexOf('All checks by layer'));
  assert.match(gaps, /No TLS fingerprint was confirmed\./);
  assert.match(gaps, /Not established/);
  assert.match(gaps, /href="#\/cockpit\/connection\/src_1"/);
  const problems = markup.slice(markup.indexOf('Established problems'), markup.indexOf('Required checks without an answer'));
  assert.match(problems, /No check reports a failure\./);
  assert.match(markup, /Incomplete: a required check has no answer yet\./);
});

test('green appears only for a live passed result, never for synthetic data', () => {
  const live = render(ready(report([result({ status: 'passed', layer: 'control_plane', code: 'control_plane.api', evidence: {}, evidenceKind: null })],
    { overall: { status: 'ready', passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' } })));
  assert.match(live, /diagnostic-status--ok/);
  const synthetic = render(ready(report([result({ status: 'passed', layer: 'capture', code: 'capture.reading', evidenceKind: 'simulation', evidence: { evidence_kind: 'simulation' } })],
    { synthetic: true, overall: { status: 'ready', passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' } })));
  assert.doesNotMatch(synthetic, /diagnostic-status--ok/);
  assert.match(synthetic, /diagnostic-status--limit/);
  assert.match(synthetic, /Synthetic report: no real system was queried/);
});

test('failed is rendered as an incident and unknown stays neutral', () => {
  const markup = render(ready(report([result(), result({ code: 'x', status: 'unknown', layer: 'network', evidenceKind: null, evidence: {} })])));
  assert.match(markup, /diagnostic-status--incident/);
  assert.match(markup, /diagnostic-status--neutral/);
});

test('layers appear in the fixed order', () => {
  const markup = render(ready(report([
    result({ code: 'a', layer: 'evidence_freshness', status: 'passed' }),
    result({ code: 'b', layer: 'tls', status: 'passed' }),
    result({ code: 'c', layer: 'control_plane', status: 'passed' }),
  ])));
  const order = ['>TLS<', '>Control plane<', '>Evidence freshness<'].map((label) => markup.indexOf(label, markup.indexOf('All checks by layer')));
  assert.ok(order.every((position) => position > 0), JSON.stringify(order));
  assert.deepEqual([...order].sort((a, b) => a - b), order);
});

test('the screen offers no repair action and no controls panel', () => {
  const markup = render(ready(report([result()])));
  assert.doesNotMatch(markup, /controls-panel/);
  for (const label of ['Suspend', 'Resume', 'Remove', 'Replay', 'Restart', 'Retry', 'Fix', 'Repair']) {
    assert.doesNotMatch(markup, new RegExp(`>${label}`), label);
  }
  const withoutShortcuts = markup.replace(/<nav class="function-key-bar"[\s\S]*?<\/nav>/, '');
  const buttons = [...withoutShortcuts.matchAll(/<button[^>]*>([^<]*)</g)].map((match) => match[1]);
  assert.deepEqual(buttons.sort(), ['Prepare diagnostic export', 'Refresh'].sort());
});

test('a pipeline filter is named, linked back to the table and can be cleared', () => {
  const markup = render(ready(report([result()]), 'pl_123'), { pipelineId: 'pl_123' });
  assert.match(markup, /Filtered to pipeline/);
  assert.match(markup, /pl_123/);
  assert.match(markup, /href="#\/cockpit\/diagnostics">Show all pipelines</);
  assert.match(markup, /href="#\/cockpit\/table\/pl_123\/proofs"/);
});

test('an empty report says so instead of reading as passed', () => {
  const markup = render(ready(report([], { overall: { status: 'ready', passed: 0, failed: 0, unknown: 0, skipped: 0, summary: '' } })));
  assert.match(markup, /The service returned no check\./);
});

test('a disagreement between service counts and listed results is disclosed', () => {
  const markup = render(ready(report([result()], { overall: { status: 'ready', passed: 5, failed: 0, unknown: 0, skipped: 0, summary: '' } })));
  assert.match(markup, /counts sent by the service differ/);
  assert.match(markup, /Blocked/);
});

function exportPayload(): DiagnosticExport {
  return {
    schema: 'quadringent.diagnostic-export.v1',
    generatedAt: '2026-10-12T19:00:00Z',
    preview: {
      files: [{ name: 'report.json', bytes: 1234, sha256: 'ab'.repeat(32) }],
      omitted: [{ field: 'source.ibmi_host', reason: 'pseudonymized by default' }],
      pseudonymized: { hosts: 1, accounts: 2, identifiers: 4 },
      limits: ['Not a reconciliation.'],
    },
    files: { 'report.json': '{}' },
  };
}

test('before the export is prepared only the prepare button exists', () => {
  const markup = render(ready(report([result()])));
  assert.match(markup, />Prepare diagnostic export</);
  assert.doesNotMatch(markup, />Download</);
});

test('the export preview lists files with sizes and digests, omissions, pseudonyms and limits before any download', () => {
  const markup = render(ready(report([result()])), { exportState: { status: 'ready', exported: exportPayload(), integrity: { problems: [], verified: true } } });
  assert.match(markup, /report\.json/);
  assert.match(markup, /1234/);
  assert.match(markup, new RegExp('ab'.repeat(32)));
  assert.match(markup, /source\.ibmi_host/);
  assert.match(markup, /pseudonymized by default/);
  assert.match(markup, /Hosts[\s\S]*1/);
  assert.match(markup, /Accounts[\s\S]*2/);
  assert.match(markup, /Not a reconciliation\./);
  assert.match(markup, />Download</);
  assert.doesNotMatch(markup, /<button[^>]*disabled=""[^>]*>Download</);
});

test('an export that does not match its preview cannot be downloaded', () => {
  const markup = render(ready(report([result()])), {
    exportState: { status: 'ready', exported: exportPayload(), integrity: { problems: ['report.json has a different SHA-256 than previewed.'], verified: true } },
  });
  assert.match(markup, /does not match its own preview/);
  assert.match(markup, /different SHA-256/);
  assert.match(markup, /<button[^>]*disabled=""[^>]*>Download</);
});

test('an unverifiable digest is disclosed without blocking the download', () => {
  const markup = render(ready(report([result()])), { exportState: { status: 'ready', exported: exportPayload(), integrity: { problems: [], verified: false } } });
  assert.match(markup, /cannot compute SHA-256/);
  assert.doesNotMatch(markup, /<button[^>]*disabled=""[^>]*>Download</);
});

test('preparing and failed export states are explicit, and a completed download is acknowledged', () => {
  assert.match(render(ready(report([result()])), { exportState: { status: 'preparing' } }), /Preparing the export/);
  const failed = render(ready(report([result()])), { exportState: { status: 'failed', failure: { kind: 'forbidden', title: 'Not allowed', detail: 'Ask for read access.', serviceMessage: null, retryAfterSeconds: null } } });
  assert.match(failed, /Not allowed/);
  assert.match(failed, /Ask for read access\./);
  const done = render(ready(report([result()])), { exportState: { status: 'ready', exported: exportPayload(), integrity: { problems: [], verified: true } }, downloaded: true });
  assert.match(done, /created in memory/);
});

test('the function key bar offers back and refresh, with no control target on this screen', () => {
  const markup = render(ready(report([result()])));
  assert.match(markup, /aria-keyshortcuts="F3"/);
  assert.match(markup, /aria-keyshortcuts="F5"/);
});

test('accessibility: one h1, labelled sections, a word on every status and native disclosure controls', () => {
  const markup = render(ready(report([result(), result({ code: 'p', layer: 'tls', status: 'passed' })])));
  assert.equal((markup.match(/<h1[ >]/g) ?? []).length, 1);
  for (const id of ['diag-problems', 'diag-gaps', 'diag-layers', 'diag-export']) {
    assert.match(markup, new RegExp(`aria-labelledby="${id}"`));
    assert.match(markup, new RegExp(`<h2 id="${id}"`));
  }
  const statuses = [...markup.matchAll(/<span class="diagnostic-status [^"]*">([^<]+)</g)].map((match) => match[1]);
  assert.ok(statuses.length >= 3);
  assert.ok(statuses.every((word) => /^(Passed|Failed|Not established|Skipped)$/.test(word!)), statuses.join(','));
  assert.match(markup, /<details class="diagnostic-result__details"><summary>/);
  assert.doesNotMatch(markup, /tabindex="[1-9]/i);
});

test('theme and focus: the diagnostics styles use only tokens and never remove the focus ring', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../styles/cockpit.css', import.meta.url), 'utf8');
  const block = css.slice(css.indexOf('Diagnostics: read-only evidence by layer'));
  assert.ok(block.length > 500);
  const colours = block.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [];
  assert.deepEqual([...new Set(colours)], ['#ffffff'], 'only the text on the primary button may be a literal colour');
  assert.doesNotMatch(block, /outline:\s*(none|0)/);
  const base = readFileSync(new URL('../../styles/base.css', import.meta.url), 'utf8');
  assert.match(base, /:focus-visible\s*{[^}]*outline:\s*2px solid var\(--focus-ring\)/);
});

const recordPass = (overrides: Partial<DiagnosticResult> = {}) => result({
  code: 'destination.verification', layer: 'destination', status: 'passed', provenance: 'control-plane:destination-record',
  subject: { kind: 'destination', ref: 'dst_1', label: null }, evidence: {}, evidenceKind: null,
  limits: 'Recorded verification state; this diagnostic does not connect to Snowflake.', ...overrides,
});

test('a passed record read shows its limit in plain sight and is labelled "Read at"', () => {
  const markup = render(ready(report([recordPass()], { overall: { status: 'ready', passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' } })));
  const layers = markup.slice(markup.indexOf('All checks by layer'));
  const beforeDetails = layers.slice(0, layers.indexOf('<details class="diagnostic-result__details"'));
  assert.match(beforeDetails, /Recorded state/);
  assert.match(beforeDetails, /Recorded verification state; this diagnostic does not connect to Snowflake\./);
  assert.match(layers, /<dt>Read at<\/dt>/);
  assert.doesNotMatch(layers, /<dt>Observed at<\/dt>/);
  assert.doesNotMatch(markup, /diagnostic-status--ok/);
});

test('an observed result keeps "Observed at" and does not repeat its limits outside the disclosure', () => {
  const markup = render(ready(report([result({ status: 'passed', provenance: 'control-plane:source-probe', evidenceKind: null, evidence: {} })])));
  const layers = markup.slice(markup.indexOf('All checks by layer'));
  assert.match(layers, /<dt>Observed at<\/dt>/);
  assert.doesNotMatch(layers.slice(0, layers.indexOf('<details class="diagnostic-result__details"')), /Collected by the loader log/);
});

test('a synthetic pass reads incomplete in the overview, never ready', () => {
  const markup = render(ready(report([result({ status: 'passed', evidenceKind: 'simulation' })], { overall: { status: 'ready', passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' } })));
  assert.match(markup, /Incomplete: a required check passed only on synthetic or historical evidence/);
  assert.doesNotMatch(markup, /No required check failed or lacks an answer/);
});

test('a pipeline count is labelled "Pipelines" in the evidence', () => {
  const truncated = result({ code: 'control_plane.report_truncated', layer: 'control_plane', status: 'unknown', subject: null, evidence: { pipeline_count: 120 }, evidenceKind: null });
  const markup = render(ready(report([truncated], { overall: { status: 'incomplete', passed: 0, failed: 0, unknown: 1, skipped: 0, summary: '' } })));
  assert.match(markup, /<span>Pipelines<\/span> <span class="mono">120<\/span>/);
  assert.doesNotMatch(markup, /Pipeline count/);
});

function bundleWith(results: number): DiagnosticExport {
  const exported = exportPayload();
  return { ...exported, files: { 'report.json': JSON.stringify({ results: Array.from({ length: results }, () => ({})) }) } };
}

test('the export preview says it covers the whole organization and how many results it holds', () => {
  const markup = render(ready(report([result()])), { exportState: { status: 'ready', exported: bundleWith(42), integrity: { problems: [], verified: true } } });
  assert.match(markup, /covers the whole organization: 42 results\./);
  assert.doesNotMatch(markup, /filtered to pipeline[^<]*the export is not/);
});

test('when the screen is filtered to one pipeline the preview says the export is not', () => {
  const markup = render(ready(report([result()]), 'pl_123'), {
    pipelineId: 'pl_123',
    exportState: { status: 'ready', exported: bundleWith(1), integrity: { problems: [], verified: true } },
  });
  assert.match(markup, /covers the whole organization: 1 result\./);
  assert.match(markup, /This screen is filtered to pipeline pl_123; the export is not\./);
});

test('an export whose size cannot be read still states its scope without inventing a count', () => {
  const markup = render(ready(report([result()])), { exportState: { status: 'ready', exported: exportPayload(), integrity: { problems: [], verified: true } } });
  assert.match(markup, /covers the whole organization\./);
  assert.doesNotMatch(markup, /organization: \d/);
});

test('the preview introduction describes what the preview shows, not the file content', () => {
  const markup = render(ready(report([result()])), { exportState: { status: 'ready', exported: exportPayload(), integrity: { problems: [], verified: true } } });
  assert.match(markup, /Review before downloading\. Nothing is stored in this browser\./);
  assert.doesNotMatch(markup, /Review what the file contains/);
});

test('the scope label comes from operator copy', () => {
  assert.match(render(ready(report([result()]))), /<dt>Scope<\/dt>/);
});
