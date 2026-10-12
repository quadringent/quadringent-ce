import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { CockpitTableData, CockpitTableState } from '../../data/useCockpit.ts';

let vite: ViteDevServer;
let CockpitTableView: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  CockpitTableView = (await vite.ssrLoadModule('/src/screens/cockpit/CockpitTable.tsx')).CockpitTableView;
});

after(async () => { await vite.close(); });

function tableData(overrides: Partial<CockpitTableData> = {}): CockpitTableData {
  return {
    pipeline: { id: 'pl_sales_order_lines', declaredState: 'attention' },
    metrics: { window: '1h', points: [{ at: '2026-09-23T09:00:00Z', lagSeconds: 12, throughputRowsPerSecond: 3 }] },
    logs: [
      { at: '2026-09-23T09:00:00Z', level: 'info', message: "Window confirmed.", incidentId: null },
      { at: '2026-09-23T09:10:00Z', level: 'error', message: "Connection refused.", incidentId: 'inc_1' },
    ],
    costToday: { scope: 'table', id: 'pl_sales_order_lines', window: '24h', status: 'estimated', amount: 0.34, currency: 'USD', basis: 'estimation_debit_moyen', collectedAt: '2026-09-23T09:00:00Z' },
    ...overrides,
  };
}

function render(props: Record<string, unknown>): string {
  return renderToStaticMarkup(createElement(CockpitTableView, {
    pipelineId: 'pl_sales_order_lines',
    tab: 'metrics',
    state: { status: 'ready', data: tableData() } as CockpitTableState,
    window: '1h',
    onWindowChange() {},
    level: undefined,
    onLevelChange() {},
    correlateIncident: false,
    onCorrelateIncidentChange() {},
    onRefresh() {},
    ...props,
  }));
}

test('all five tabs are always present and the active one is marked current', () => {
  const markup = render({ tab: 'logs' });
  for (const label of ["Metrics", "Latest rows", "Logs", "Costs", "Evidence"]) {
    assert.match(markup, new RegExp(`>${label}<`));
  }
  assert.match(markup, /aria-current="page" href="#\/cockpit\/table\/pl_sales_order_lines\/logs">Logs</);
});

test('an accepted copy displays its real pending reconciliation reason', () => {
  const data = tableData({ pipeline: { id: 'pl_sales_order_lines', declaredState: 'copying',
    attentionReason: "Copy started; reader or loader reconciliation pending." } });
  const markup = render({ state: { status: 'ready', data } });
  assert.match(markup, /role="status">Copy started; reader or loader reconciliation pending/);
});

test('the metrics tab charts render, and the rows tab never leaks row values, only timestamped activity', () => {
  const metrics = render({ tab: 'metrics' });
  assert.match(metrics, /Lag/);
  assert.match(metrics, /Throughput/);

  const rows = render({ tab: 'rows' });
  assert.match(rows, /row values are never displayed/);
  assert.match(rows, /Window confirmed/);
  assert.doesNotMatch(rows, /Connection refused/, "only info-level entries belong in \"Latest rows\"");
});

test('loader telemetry identifies delivery latency and the bounded source', () => {
  const data = tableData({
    metrics: {
      window: '1h',
      points: [{ at: '2026-09-28T21:04:00Z', lagSeconds: 8.1, throughputRowsPerSecond: null }],
      provenance: 'journal_chargeur_kubernetes',
      freshness: "limited to the last 2000 pod log lines",
      reason: "Lag is the IBM i → MERGE delay of delivered batches; the series is limited to the retained log.",
    },
  });
  const markup = render({ state: { status: 'ready', data } });
  assert.match(markup, /IBM i → mirror delay/);
  assert.match(markup, /limited to the last 2000 pod log lines/);
});

test('the logs tab shows every entry with its incident tag, and exposes level/correlation filter controls', () => {
  const markup = render({ tab: 'logs' });
  assert.match(markup, /Window confirmed/);
  assert.match(markup, /Connection refused/);
  assert.match(markup, /inc_1/);
  assert.match(markup, /Incident-correlated only/);
});

test('the costs tab distinguishes measured/estimated from absent, and the proofs tab is honestly absent for v2', () => {
  const estimated = render({ tab: 'costs' });
  assert.match(estimated, /estimated/);
  const absent = render({ tab: 'costs', state: { status: 'ready', data: tableData({ costToday: null }) } });
  assert.match(absent, /Absent/);

  const proofs = render({ tab: 'proofs' });
  assert.match(proofs, /Absent/);
  assert.doesNotMatch(proofs, /reconcil/i);
});

test('loading and failed states never render tab content or the controls panel', () => {
  const loading = render({ state: { status: 'loading' } });
  assert.match(loading, /Reading/);
  assert.doesNotMatch(loading, /cockpit-table__tab"/);

  const failed = render({ state: { status: 'failed', message: 'panne v2' } });
  assert.match(failed, /Service unavailable/);
  assert.match(failed, /panne v2/);
});

test('the table controls panel offers all five contract actions', () => {
  const markup = render({ tab: 'metrics' });
  for (const label of ["Suspend", "Resume", "Restart the initial copy", "Remove", "Replay a range"]) {
    assert.match(markup, new RegExp(`>${label}<`));
  }
});

test('the function key bar is present and F12 jumps to the logs tab hash', () => {
  const markup = render({ tab: 'metrics' });
  assert.match(markup, /function-key-bar/);
  assert.match(markup, /aria-keyshortcuts="F12"/);
});

test('evidence shows dated per-table delivery and keeps reconciliation unmeasured', () => {
  const absent = { state: 'absent', observedAt: null, eventCount: null, lagSeconds: null, duplicateCount: null, detail: 'Independent reconciliation not measured.' };
  const data = tableData({ evidence: {
    pipelineId: 'pl_sales_order_lines', provenance: 'kubernetes_loader_log', evidenceKind: 'live',
    collectedAt: '2026-10-10T12:00:00Z', reading: absent, reconciliation: absent,
    delivery: { ...absent, state: 'observed', observedAt: '2026-10-10T11:59:30Z',
      eventCount: 3, lagSeconds: 6.2, detail: 'Last delivered batch; retained logs only.' },
  } });
  const markup = render({ tab: 'proofs', state: { status: 'ready', data } });
  assert.match(markup, /6.2 s/);
  assert.match(markup, /2026-10-10T11:59:30Z/);
  assert.match(markup, /Independent reconciliation not measured/);
  assert.doesNotMatch(markup, /0 duplicates|No duplicates|Certified/);
});

test('simulation and stale evidence remain explicit on the evidence screen', () => {
  const stage = { state: 'stale', observedAt: '2026-10-09T12:00:00Z', eventCount: 12, lagSeconds: null, duplicateCount: null, detail: 'Historical measurement.' };
  const data = tableData({ evidence: {
    pipelineId: 'pl_sales_order_lines', provenance: 'demo_fixture', evidenceKind: 'simulation',
    collectedAt: '2026-10-09T12:00:00Z', reading: stage, delivery: stage, reconciliation: stage,
  } });
  const markup = render({ tab: 'proofs', state: { status: 'ready', data } });
  assert.match(markup, /Demonstration — synthetic data/);
  assert.match(markup, /Stale/);
});

test('an evidence request failure leaves the other table tabs usable', () => {
  const data = tableData({ evidenceError: 'Evidence service unavailable.' });
  const markup = render({ tab: 'proofs', state: { status: 'ready', data } });
  assert.match(markup, /Evidence service unavailable/);
  assert.match(render({ tab: 'metrics', state: { status: 'ready', data } }), /Throughput/);
});

test('evidence identifies a producer version only when reported by the service', () => {
  const stage = { state: 'absent' as const, observedAt: null, eventCount: null, lagSeconds: null, duplicateCount: null, detail: 'Not measured.' };
  for (const producerVersion of [null, 'synthetic-build']) {
    const data = tableData({ evidence: {
      pipelineId: 'pl_sales_order_lines', provenance: 'synthetic_fixture', evidenceKind: 'simulation',
      collectedAt: null, reading: stage, delivery: stage, reconciliation: stage, producerVersion,
    } });
    const markup = render({ tab: 'proofs', state: { status: 'ready', data } });
    assert.match(markup, /Producer version/);
    assert.match(markup, producerVersion ? /synthetic-build/ : /Not reported by the service/);
  }
});

test('evidence stages show a word and tone for each state, with no green or time for missing stages', () => {
  const absent = { state: 'absent', observedAt: null, eventCount: null, lagSeconds: null, duplicateCount: null, detail: 'Not measured.' };
  const data = tableData({ evidence: {
    pipelineId: 'pl_sales_order_lines', provenance: 'kubernetes_loader_log', evidenceKind: 'live',
    collectedAt: '2026-10-10T12:00:00Z',
    reading: { ...absent, state: 'observed', observedAt: '2026-10-10T11:59:00Z', eventCount: 4, detail: 'Read.' },
    delivery: { ...absent, state: 'stale', observedAt: '2026-10-10T10:00:00Z', eventCount: 2, detail: 'Old.' },
    reconciliation: absent,
  } });
  const markup = render({ tab: 'proofs', state: { status: 'ready', data } });
  assert.match(markup, /evidence-state--observed[^>]*>Observed/);
  assert.match(markup, /evidence-state--limit[^>]*>Stale/);
  assert.match(markup, /evidence-state--missing[^>]*>Not measured/);
  // The live evidence label and the one observed stage; never the stale or missing stages.
  assert.equal((markup.match(/evidence-state--observed/g) ?? []).length, 2);
});

test('synthetic evidence is never presented as an observation', () => {
  const observed = { state: 'observed', observedAt: '2026-09-23T09:00:00Z', eventCount: 120, lagSeconds: 6.2, duplicateCount: null, detail: 'Synthetic.' };
  const data = tableData({ evidence: {
    pipelineId: 'pl_sales_order_lines', provenance: 'demo_fixture', evidenceKind: 'simulation',
    collectedAt: '2026-09-23T09:00:00Z', reading: observed, delivery: observed,
    reconciliation: { ...observed, state: 'absent', observedAt: null, eventCount: null, lagSeconds: null },
  } });
  const markup = render({ tab: 'proofs', state: { status: 'ready', data } });
  assert.doesNotMatch(markup, /evidence-state--observed/);
  assert.match(markup, /do not come from your system/);
  assert.match(markup, /Provenance/);
  assert.match(markup, /demo_fixture/);
});

test('a table without collected evidence says so instead of showing empty stages as data', () => {
  const absent = { state: 'absent', observedAt: null, eventCount: null, lagSeconds: null, duplicateCount: null, detail: 'Not measured.' };
  const data = tableData({ evidence: {
    pipelineId: 'pl_sales_order_lines', provenance: 'demo_fixture', evidenceKind: 'absent',
    collectedAt: null, reading: absent, delivery: absent, reconciliation: absent,
  } });
  const markup = render({ tab: 'proofs', state: { status: 'ready', data } });
  assert.match(markup, /No evidence collected/);
  assert.match(markup, /Collected at<\/dt><dd>—/);
});

test('an estimated cost names its basis and collection time without calling it observed', () => {
  const markup = render({ tab: 'costs' });
  assert.match(markup, /estimated/);
  assert.match(markup, /Basis: estimation_debit_moyen/);
  assert.match(markup, /Collected at <time dateTime="2026-09-23T09:00:00Z">/);
  assert.doesNotMatch(markup, /Observed on|Database:/);
});
