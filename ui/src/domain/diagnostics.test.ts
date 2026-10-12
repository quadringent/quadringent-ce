import assert from 'node:assert/strict';
import test from 'node:test';
import type { DiagnosticReport, DiagnosticResult } from '../data/diagnosticsContract.ts';
import {
  buildDiagnosticsView,
  describeDiagnosticsError,
  diagnosticTone,
  evidenceRows,
  limitQualifier,
  subjectRoute,
} from './diagnostics.ts';

function result(overrides: Partial<DiagnosticResult> = {}): DiagnosticResult {
  return {
    code: 'delivery.snowflake',
    layer: 'delivery',
    status: 'passed',
    required: true,
    observedAt: '2026-10-12T18:59:30Z',
    provenance: 'control-plane:evidence',
    subject: { kind: 'pipeline', ref: 'pl_123', label: 'SALES.ORDERS' },
    since: null,
    summary: 'Delivery observed.',
    nextAction: null,
    limits: null,
    evidence: {},
    evidenceKind: null,
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
    overall: { status: 'ready', passed: 0, failed: 0, unknown: 0, skipped: 0, summary: '' },
    results,
    ...overrides,
  };
}

test('passed is green only for a non-synthetic result whose evidence kind is absent or live', () => {
  const live = report([]);
  assert.equal(diagnosticTone(result({ status: 'passed', evidenceKind: null }), live), 'ok');
  assert.equal(diagnosticTone(result({ status: 'passed', evidenceKind: 'live' }), live), 'ok');
  assert.equal(diagnosticTone(result({ status: 'passed', evidenceKind: 'simulation' }), live), 'limit');
  assert.equal(diagnosticTone(result({ status: 'passed', evidenceKind: 'historical' }), live), 'limit');
  assert.equal(diagnosticTone(result({ status: 'passed', evidenceKind: 'absent' }), live), 'limit');
  assert.equal(diagnosticTone(result({ status: 'passed', evidenceKind: 'live' }), report([], { synthetic: true })), 'limit');
});

test('failed is always an incident, unknown and skipped are neutral unless synthetic or historical', () => {
  const live = report([]);
  assert.equal(diagnosticTone(result({ status: 'failed' }), live), 'incident');
  assert.equal(diagnosticTone(result({ status: 'failed', evidenceKind: 'simulation' }), live), 'incident');
  assert.equal(diagnosticTone(result({ status: 'unknown' }), live), 'neutral');
  assert.equal(diagnosticTone(result({ status: 'skipped' }), live), 'neutral');
  assert.equal(diagnosticTone(result({ status: 'unknown', evidenceKind: 'simulation' }), live), 'limit');
  assert.equal(diagnosticTone(result({ status: 'unknown' }), report([], { synthetic: true })), 'limit');
});

test('the qualifier names why a result is limited and is null for live evidence', () => {
  assert.equal(limitQualifier(result({ evidenceKind: 'live' }), false), null);
  assert.equal(limitQualifier(result({}), false), null);
  assert.equal(limitQualifier(result({ evidenceKind: 'simulation' }), false), 'Synthetic');
  assert.equal(limitQualifier(result({ evidenceKind: 'historical' }), false), 'Historical');
  assert.equal(limitQualifier(result({ evidenceKind: 'absent' }), false), 'No evidence collected');
  assert.equal(limitQualifier(result({ evidenceKind: 'live' }), true), 'Synthetic');
});

test('results are grouped by layer in the fixed order and empty layers are omitted', () => {
  const view = buildDiagnosticsView(report([
    result({ code: 'z', layer: 'evidence_freshness' }),
    result({ code: 'a', layer: 'control_plane' }),
    result({ code: 'b', layer: 'tls' }),
    result({ code: 'c', layer: 'delivery' }),
    result({ code: 'd', layer: 'local_tools' }),
    result({ code: 'e', layer: 'capture' }),
  ]));
  assert.deepEqual(view.groups.map((group) => group.layer), [
    'local_tools', 'tls', 'control_plane', 'capture', 'delivery', 'evidence_freshness',
  ]);
  assert.deepEqual(view.groups.map((group) => group.label), [
    'Local tools', 'TLS', 'Control plane', 'Capture', 'Delivery', 'Evidence freshness',
  ]);
});

test('within a layer the problems come first: failed, then unknown, skipped, passed', () => {
  const view = buildDiagnosticsView(report([
    result({ code: 'p', layer: 'tls', status: 'passed' }),
    result({ code: 's', layer: 'tls', status: 'skipped' }),
    result({ code: 'u', layer: 'tls', status: 'unknown' }),
    result({ code: 'f', layer: 'tls', status: 'failed' }),
  ]));
  assert.deepEqual(view.groups[0]!.views.map((item) => item.result.code), ['f', 'u', 's', 'p']);
});

test('established problems list failed results first, then required unknown or skipped ones as gaps', () => {
  const view = buildDiagnosticsView(report([
    result({ code: 'unknown-required', layer: 'network', status: 'unknown', required: true }),
    result({ code: 'unknown-optional', layer: 'network', status: 'unknown', required: false }),
    result({ code: 'failed-late', layer: 'delivery', status: 'failed' }),
    result({ code: 'failed-early', layer: 'tls', status: 'failed' }),
    result({ code: 'skipped-required', layer: 'capture', status: 'skipped', required: true }),
    result({ code: 'passed', layer: 'tls', status: 'passed' }),
  ]));
  assert.deepEqual(view.problems.map((item) => item.result.code), ['failed-early', 'failed-late']);
  assert.deepEqual(view.gaps.map((item) => item.result.code), ['unknown-required', 'skipped-required']);
  assert.ok(view.problems.every((item) => item.established === 'problem'));
  assert.ok(view.gaps.every((item) => item.established === 'gap'));
});

test('counts come from the listed results and a disagreeing service count is flagged', () => {
  const results = [result({ status: 'passed' }), result({ status: 'failed' }), result({ status: 'unknown' })];
  const consistent = buildDiagnosticsView(report(results, {
    overall: { status: 'blocked', passed: 1, failed: 1, unknown: 1, skipped: 0, summary: 'x' },
  }));
  assert.deepEqual(consistent.counts, { passed: 1, failed: 1, unknown: 1, skipped: 0, total: 3 });
  assert.equal(consistent.countsConsistent, true);
  const inconsistent = buildDiagnosticsView(report(results, {
    overall: { status: 'ready', passed: 3, failed: 0, unknown: 0, skipped: 0, summary: 'x' },
  }));
  assert.deepEqual(inconsistent.counts, { passed: 1, failed: 1, unknown: 1, skipped: 0, total: 3 });
  assert.equal(inconsistent.countsConsistent, false);
});

test('the overall verdict never reads healthy and a service that understates a problem is overruled', () => {
  const failing = buildDiagnosticsView(report([result({ status: 'failed' })], {
    overall: { status: 'ready', passed: 0, failed: 1, unknown: 0, skipped: 0, summary: 'fine' },
  }));
  assert.equal(failing.overall.status, 'blocked');
  assert.match(failing.overall.text, /Blocked/);
  const incomplete = buildDiagnosticsView(report([result({ status: 'unknown', required: true })], {
    overall: { status: 'ready', passed: 0, failed: 0, unknown: 1, skipped: 0, summary: '' },
  }));
  assert.equal(incomplete.overall.status, 'incomplete');
  const ready = buildDiagnosticsView(report([result({ status: 'passed' })], {
    overall: { status: 'ready', passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' },
  }));
  assert.equal(ready.overall.status, 'ready');
  for (const view of [failing, incomplete, ready]) {
    assert.doesNotMatch(JSON.stringify(view.overall), /healthy/i);
  }
  const synthetic = buildDiagnosticsView(report([result({ status: 'passed' })], {
    synthetic: true,
    overall: { status: 'ready', passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' },
  }));
  assert.equal(synthetic.overall.status, 'incomplete', 'synthetic data never reads as ready');
  assert.match(synthetic.overall.text, /synthetic/i);
});

test('a worse overall status sent by the service is kept, never softened', () => {
  const view = buildDiagnosticsView(report([result({ status: 'passed' })], {
    overall: { status: 'blocked', passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' },
  }));
  assert.equal(view.overall.status, 'blocked');
});

test('a pipeline subject links to its table screen and a source subject to its connection', () => {
  assert.deepEqual(subjectRoute({ kind: 'pipeline', ref: 'pl_1', label: null }), { name: 'cockpit-table', id: 'pl_1' });
  assert.deepEqual(subjectRoute({ kind: 'source', ref: 'src_1', label: null }), { name: 'cockpit-connection', id: 'src_1' });
  for (const kind of ['table', 'destination', 'host', 'tool', 'control_plane'] as const) {
    assert.equal(subjectRoute({ kind, ref: 'x', label: null }), null, kind);
  }
  assert.equal(subjectRoute(null), null);
});

test('views carry the subject label, the status word and the link', () => {
  const view = buildDiagnosticsView(report([
    result({ status: 'failed', since: '2026-10-12T18:40:00Z', nextAction: 'Open the logs.' }),
  ]));
  const item = view.problems[0]!;
  assert.equal(item.statusWord, 'Failed');
  assert.equal(item.tone, 'incident');
  assert.equal(item.subjectText, 'Table: SALES.ORDERS');
  assert.deepEqual(item.subjectRoute, { name: 'cockpit-table', id: 'pl_123' });
  assert.equal(item.sinceText, '2026-10-12 18:40:00 UTC');
  assert.equal(item.observedText, '2026-10-12 18:59:30 UTC');
});

test('a missing subject label falls back to the reference, and no observation time says so', () => {
  const item = buildDiagnosticsView(report([
    result({ status: 'unknown', subject: { kind: 'destination', ref: 'dst_9', label: null }, observedAt: null }),
  ])).gaps[0]!;
  assert.equal(item.subjectText, 'Destination: dst_9');
  assert.equal(item.observedText, null);
});

test('evidence rows hide the evidence kind, keep scalar values exact and never invent a value', () => {
  const rows = evidenceRows(result({
    evidence: { stage_state: 'stale', evidence_kind: 'live', lag_seconds: 6.2, reconciled: false, note: null },
  }));
  assert.deepEqual(rows, [
    { key: 'stage_state', label: 'Stage state', value: 'stale' },
    { key: 'lag_seconds', label: 'Lag seconds', value: '6.2' },
    { key: 'reconciled', label: 'Reconciled', value: 'no' },
    { key: 'note', label: 'Note', value: '—' },
  ]);
});

test('a synthetic capture result that claims passed stays amber with an explicit qualifier', () => {
  const view = buildDiagnosticsView(report([
    result({ layer: 'capture', code: 'capture.reading', status: 'passed', evidenceKind: 'simulation' }),
  ]));
  const item = view.groups[0]!.views[0]!;
  assert.equal(item.tone, 'limit');
  assert.equal(item.qualifier, 'Synthetic');
  assert.equal(item.statusWord, 'Passed');
});

test('an empty report has no groups and no problems, and is not called ready by omission', () => {
  const view = buildDiagnosticsView(report([]));
  assert.deepEqual(view.groups, []);
  assert.deepEqual(view.problems, []);
  assert.equal(view.counts.total, 0);
  assert.equal(view.empty, true);
});

test('each failure kind has its own title and instruction, and Retry-After survives only where it applies', () => {
  const titles = new Set<string>();
  for (const kind of ['session', 'forbidden', 'not_found', 'rate_limited', 'unavailable', 'invalid_response'] as const) {
    const failure = describeDiagnosticsError({ kind, message: 'raw', retryAfterSeconds: 12 });
    assert.equal(failure.kind, kind);
    titles.add(failure.title);
  }
  assert.equal(titles.size, 6);
  assert.equal(describeDiagnosticsError({ kind: 'rate_limited', retryAfterSeconds: 12 }).retryAfterSeconds, 12);
  assert.equal(describeDiagnosticsError({ kind: 'rate_limited' }).retryAfterSeconds, null);
  assert.equal(describeDiagnosticsError({ kind: 'forbidden', retryAfterSeconds: 12 }).retryAfterSeconds, null);
  assert.match(describeDiagnosticsError({ kind: 'session' }).detail, /Sign in again/);
  assert.match(describeDiagnosticsError({ kind: 'forbidden' }).detail, /read access/);
});

test('an unexpected error reads as an unavailable service, never as an empty or healthy report', () => {
  for (const error of [new Error('boom'), 'text', null, undefined, {}]) {
    const failure = describeDiagnosticsError(error);
    assert.equal(failure.kind, 'unavailable');
    assert.equal(failure.serviceMessage, null);
  }
});

test('the service message is surfaced for an unreadable report, not for a plain outage', () => {
  assert.equal(describeDiagnosticsError({ kind: 'invalid_response', message: 'bad layer' }).serviceMessage, 'bad layer');
  assert.equal(describeDiagnosticsError({ kind: 'unavailable', message: 'internal detail' }).serviceMessage, null);
});

const recordRead = (overrides: Partial<DiagnosticResult> = {}) => result({
  code: 'destination.verification', layer: 'destination', provenance: 'control-plane:destination-record',
  subject: { kind: 'destination', ref: 'dst_1', label: null }, ...overrides,
});

test('a passed result read from a record is not a fresh observation: amber, labelled, never green', () => {
  for (const provenance of ['control-plane:source-record', 'control-plane:destination-record', 'control-plane:pipeline-record']) {
    const item = buildDiagnosticsView(report([recordRead({ provenance, code: 'x.record', status: 'passed' })])).groups[0]!.views[0]!;
    assert.equal(item.tone, 'limit', provenance);
    assert.equal(item.qualifier, 'Recorded state', provenance);
    assert.equal(item.recordRead, true);
    assert.equal(item.observedLabel, 'Read at');
  }
  const probe = buildDiagnosticsView(report([recordRead({ provenance: 'control-plane:source-probe', status: 'passed' })])).groups[0]!.views[0]!;
  assert.equal(probe.tone, 'ok');
  assert.equal(probe.recordRead, false);
  assert.equal(probe.observedLabel, 'Observed at');
});

test('a failed or unknown record result keeps its own tone but is still labelled as a read', () => {
  const failed = buildDiagnosticsView(report([recordRead({ status: 'failed' })])).problems[0]!;
  assert.equal(failed.tone, 'incident');
  assert.equal(failed.observedLabel, 'Read at');
  const unknown = buildDiagnosticsView(report([recordRead({ status: 'unknown' })])).gaps[0]!;
  assert.equal(unknown.tone, 'neutral');
});

test('the declared pipeline state is neutral, not green, even when it passes', () => {
  const item = buildDiagnosticsView(report([recordRead({
    code: 'pipeline.state', layer: 'capture', provenance: 'control-plane:pipeline-record', required: false, status: 'passed',
    subject: { kind: 'pipeline', ref: 'pl_1', label: null },
  })])).groups[0]!.views[0]!;
  assert.equal(item.tone, 'neutral');
  assert.equal(item.qualifier, 'Recorded state');
});

test('ready becomes incomplete when a required pass rests on synthetic or historical evidence', () => {
  const serverReady = { status: 'ready' as const, passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' };
  for (const evidenceKind of ['simulation', 'historical'] as const) {
    const view = buildDiagnosticsView(report([result({ status: 'passed', required: true, evidenceKind })], { overall: serverReady }));
    assert.equal(view.overall.status, 'incomplete', evidenceKind);
    assert.match(view.overall.text, /passed on synthetic or historical evidence/);
  }
  const optional = buildDiagnosticsView(report([result({ status: 'passed', required: false, evidenceKind: 'simulation' })], { overall: serverReady }));
  assert.equal(optional.overall.status, 'ready', 'an optional synthetic pass does not weaken a verdict');
  const live = buildDiagnosticsView(report([result({ status: 'passed', required: true, evidenceKind: 'live' })], { overall: serverReady }));
  assert.equal(live.overall.status, 'ready');
  const recorded = buildDiagnosticsView(report([recordRead({ status: 'passed' })], { overall: serverReady }));
  assert.equal(recorded.overall.status, 'ready', 'a recorded state is limited but stays a pass of what it states');
});

test('a failed result still reads blocked when a synthetic pass sits next to it', () => {
  const view = buildDiagnosticsView(report([
    result({ status: 'passed', required: true, evidenceKind: 'simulation' }),
    result({ code: 'f', status: 'failed' }),
  ]));
  assert.equal(view.overall.status, 'blocked');
});

test('evidence labels use operator wording for the pipeline count', () => {
  const rows = evidenceRows(result({ evidence: { pipeline_count: 120, event_count: 3 } }));
  assert.deepEqual(rows.map((row) => [row.label, row.value]), [['Pipelines', '120'], ['Event count', '3']]);
});
