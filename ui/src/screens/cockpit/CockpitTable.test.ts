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
