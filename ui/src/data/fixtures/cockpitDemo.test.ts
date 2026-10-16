import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlPlaneV2Client, pendingConfirmationId } from '../controlPlaneV2Client.ts';
import {
  createCockpitDemoFetch,
  DEMO_SOURCE_SALES,
  DEMO_SOURCE_INVENTORY,
  DEMO_DESTINATION_SALES,
  demoNameForPipeline,
  demoSourceIdForPipeline,
} from './cockpitDemo.ts';
import { buildConnections, connectionAggregateState, groupTablesBySource } from '../../domain/cockpit.ts';
import { controlActionCopy } from '../../domain/operator.ts';
import { loadTableData } from '../cockpitLoaders.ts';

function client(): ControlPlaneV2Client {
  return new ControlPlaneV2Client({ fetchFn: createCockpitDemoFetch() });
}

test('the demo dataset has two connections, eight tables total, one incident, one paused table, one copying', async () => {
  const c = client();
  const sources = await c.listSources();
  const destinations = await c.listDestinations();
  const pipelines = await c.listPipelines();
  assert.equal(sources.length, 2);
  assert.equal(destinations.length, 2);
  assert.equal(pipelines.length, 8);

  const grouped = groupTablesBySource(pipelines, demoSourceIdForPipeline, demoNameForPipeline);
  const connections = buildConnections(sources, destinations, grouped);
  assert.equal(connections.length, 2);
  const ventes = connections.find((c) => c.id === DEMO_SOURCE_SALES)!;
  const stocks = connections.find((c) => c.id === DEMO_SOURCE_INVENTORY)!;
  assert.equal(ventes.tables.length, 4);
  assert.equal(stocks.tables.length, 4);
  assert.equal(connectionAggregateState(ventes), 'attention');
  assert.equal(connectionAggregateState(stocks), 'copying');
  assert.equal(stocks.tables.some((t) => t.declaredState === 'paused'), true);
});

test('a non-sensitive pipeline action (pause) executes directly through the demo, verified by a fresh read', async () => {
  const c = client();
  const before = await c.getPipeline('pl_sales_customers');
  assert.equal(before.declaredState, 'live');
  const envelope = await c.runPipelineAction('pl_sales_customers', 'pause');
  assert.equal(envelope.after?.declaredState, 'paused');
  const after = await c.getPipeline('pl_sales_customers');
  assert.equal(after.declaredState, 'paused');
});

test('a sensitive action (remove) requires confirmation, and approving it lets the rerun succeed', async () => {
  const c = client();
  await assert.rejects(
    () => c.runPipelineAction('pl_sales_archive', 'remove'),
    (error: unknown) => {
      const id = pendingConfirmationId(error as import('../controlPlaneV2Client.ts').ControlPlaneV2Error);
      assert.ok(id);
      return true;
    },
  );
  const pending = await c.listConfirmations('pending');
  const confirmation = pending.find((item) => item.resourceId === 'pl_sales_archive')!;
  assert.ok(confirmation);
  assert.deepEqual(confirmation.available, { approve: true, reject: true, execute: false });
  const approved = await c.approveConfirmation(confirmation.id);
  assert.equal(approved.state, 'approved');
  const readyToExecute = await c.getConfirmation(confirmation.id);
  assert.deepEqual(readyToExecute.available, { approve: false, reject: false, execute: true });
  const envelope = await c.runPipelineAction('pl_sales_archive', 'remove', { confirmationToken: confirmation.id });
  assert.equal(envelope.after?.declaredState, 'stopped');
  assert.equal((await c.getConfirmation(confirmation.id)).state, 'used');
  assert.equal((await c.getPipeline('pl_sales_archive')).declaredState, 'stopped');
});

test('dry_run never mutates state and reports the announced effect', async () => {
  const c = client();
  const before = await c.getPipeline('pl_inventory_warehouses');
  const envelope = await c.runPipelineAction('pl_inventory_warehouses', 'pause', { dryRun: true });
  assert.ok(envelope.dryRun);
  assert.equal((envelope.dryRun as { effect: string }).effect, controlActionCopy('pause').effect);
  const after = await c.getPipeline('pl_inventory_warehouses');
  assert.equal(after.declaredState, before.declaredState);
});

test('table resume preview announces delivery rather than journal reading', async () => {
  const c = client();
  const before = await c.getPipeline('pl_inventory_movements');
  const envelope = await c.runPipelineAction('pl_inventory_movements', 'resume', { dryRun: true });
  assert.equal((envelope.dryRun as { effect: string }).effect, controlActionCopy('resume').effect);
  assert.deepEqual(await c.getPipeline('pl_inventory_movements'), before);
});

for (const scope of ['source', 'destination', 'fleet'] as const) {
  for (const action of ['pause', 'resume'] as const) {
    test(`${scope} ${action} preview leaves every pipeline unchanged`, async () => {
      const c = client();
      const before = await c.listPipelines();
      const options = { dryRun: true };
      const envelope = scope === 'source'
        ? await c.runSourceAction(DEMO_SOURCE_SALES, action, options)
        : scope === 'destination'
          ? await c.runDestinationAction(DEMO_DESTINATION_SALES, action, options)
          : await c.runFleetAction(action === 'pause' ? 'pause_all' : 'resume_all', options);
      assert.ok(envelope.dryRun);
      assert.deepEqual(await c.listPipelines(), before);
    });
  }
}

test('source-level pause affects every table of that source only, and resume brings them back live', async () => {
  const c = client();
  await c.runSourceAction(DEMO_SOURCE_INVENTORY, 'pause');
  const stocksPipelines = await Promise.all(['pl_inventory_items', 'pl_inventory_warehouses', 'pl_inventory_alerts'].map((id) => c.getPipeline(id)));
  assert.ok(stocksPipelines.every((p) => p.declaredState === 'paused'));
  const ventesUnaffected = await c.getPipeline('pl_sales_customers');
  assert.equal(ventesUnaffected.declaredState, 'live');
  await c.runSourceAction(DEMO_SOURCE_INVENTORY, 'resume');
  const resumed = await c.getPipeline('pl_inventory_items');
  assert.equal(resumed.declaredState, 'live');
});

test('metrics are deterministic across two reads and absent (null) for a paused pipeline', async () => {
  const c = client();
  const first = await c.getMetrics('pl_inventory_movements', '1h');
  const second = await c.getMetrics('pl_inventory_movements', '1h');
  assert.deepEqual(first, second);
  assert.equal(first.points.length, 12);
  assert.ok(first.points.every((point) => point.lagSeconds === null));

  const live = await c.getMetrics('pl_sales_customers', '24h');
  assert.equal(live.points.length, 24);
  assert.ok(live.points.every((point) => point.lagSeconds !== null));
});

test('logs correlate the incident pipeline and never leak row data (no row/value fields)', async () => {
  const c = client();
  const all = await c.getLogs('pl_sales_order_lines');
  assert.ok(all.some((entry) => entry.incidentId !== null));
  const correlated = await c.getLogs('pl_sales_order_lines', { correlateIncident: true });
  assert.ok(correlated.length > 0);
  assert.ok(correlated.every((entry) => entry.incidentId !== null));
  for (const entry of all) {
    assert.doesNotMatch(JSON.stringify(entry), /"row"|"value"|"data"/);
  }
});

test('generated demo costs stay synthetic at both scopes, and paused costs stay absent', async () => {
  const c = client();
  const measuredConnection = await c.getCosts('connection', DEMO_SOURCE_SALES, '24h');
  assert.equal(measuredConnection.status, 'synthetic');
  const estimatedConnection = await c.getCosts('connection', DEMO_SOURCE_INVENTORY, '24h');
  assert.equal(estimatedConnection.status, 'synthetic');
  const tableCost = await c.getCosts('table', 'pl_sales_order_lines', '24h');
  assert.equal(tableCost.status, 'synthetic');
  const absentTable = await c.getCosts('table', 'pl_inventory_movements', '24h');
  assert.equal(absentTable.status, 'absent');
  assert.equal(absentTable.amount, null);
  assert.notEqual(absentTable.synthetic, true);
});

test('table evidence remains explicitly synthetic and never claims complete reconciliation', async () => {
  const fetch = createCockpitDemoFetch();
  const body = await (await fetch('/v2/pipelines/pl_sales_orders/evidence')).json();
  assert.equal(body.evidence_kind, 'simulation');
  assert.equal(body.reconciliation.state, 'absent');
  assert.equal(body.reconciliation.duplicate_count, null);
});

test('every demo pipeline explicitly has unavailable native verification and no latest result', async () => {
  const fetch = createCockpitDemoFetch();
  const c = new ControlPlaneV2Client({ fetchFn: fetch });
  const pipelines = await c.listPipelines();
  for (const pipeline of pipelines) {
    const capabilities = await fetch(`/v2/pipelines/${pipeline.id}/native-verifications/capabilities`);
    assert.equal(capabilities.status, 200);
    assert.deepEqual(await capabilities.json(), { available: false, mode: null, reason_code: 'not_configured' });
    assert.deepEqual(await c.getNativeCapabilities(pipeline.id), {
      available: false, mode: null, reasonCode: 'not_configured',
    });
    assert.equal(await c.getLatestNativeVerification(pipeline.id), null);
    const latest = await fetch(`/v2/pipelines/${pipeline.id}/native-verifications/latest`);
    assert.equal(latest.status, 200);
    assert.equal(await latest.json(), null);
  }
  assert.deepEqual(await c.listPipelines(), pipelines);
});

test('the demo table loader reports absent native proof without availability or read errors', async (t) => {
  const network = t.mock.method(globalThis, 'fetch', () => { throw new Error('Network forbidden in demonstration'); });
  const data = await loadTableData(client(), async () => ({
    sourceIdForPipeline: demoSourceIdForPipeline, nameForPipeline: demoNameForPipeline,
  }), 'pl_sales_customers', '1h', {});
  assert.equal(data.nativeCapabilitiesError, false);
  assert.equal(data.nativeVerificationError, false);
  assert.deepEqual(data.nativeCapabilities, { available: false, mode: null, reasonCode: 'not_configured' });
  assert.equal(data.nativeVerification, null);
  assert.equal(data.evidence?.evidenceKind, 'simulation');
  assert.equal(network.mock.callCount(), 0);
});

test('native demo routes keep unknown reads and every native action unavailable without network', async (t) => {
  const network = t.mock.method(globalThis, 'fetch', () => { throw new Error('Network forbidden in demonstration'); });
  const fetch = createCockpitDemoFetch();
  const c = new ControlPlaneV2Client({ fetchFn: fetch });
  const before = await c.listPipelines();
  for (const suffix of ['capabilities', 'latest']) {
    const response = await fetch(`/v2/pipelines/pl_unknown/native-verifications/${suffix}`);
    assert.equal(response.status, 404);
    assert.equal((await response.json()).error.code, 'not_found');
  }
  for (const suffix of ['unknown', 'history', 'latest/extra']) {
    assert.equal((await fetch(`/v2/pipelines/pl_sales_customers/native-verifications/${suffix}`)).status, 404);
  }
  for (const pipeline of before) {
    for (const dryRun of [true, false]) {
      await assert.rejects(() => c.requestNativeVerification(pipeline.id, dryRun),
        (error: unknown) => (error as { status: number }).status === 404);
    }
  }
  assert.equal((await fetch('/v2/pipelines/pl_sales_customers/native-verifications/capabilities', {
    method: 'POST', body: '{}',
  })).status, 404);
  assert.deepEqual(await c.listPipelines(), before);
  assert.equal(network.mock.callCount(), 0);
});

test('table evidence varies by table: stale after an incident, absent during copy, missing when never collected', async () => {
  const fetch = createCockpitDemoFetch();
  const evidence = async (id: string) => (await fetch(`/v2/pipelines/${id}/evidence`)).json();
  const incident = await evidence('pl_sales_order_lines');
  assert.equal(incident.reading.state, 'observed');
  assert.equal(incident.delivery.state, 'stale');
  assert.ok(Date.parse(incident.delivery.observed_at) < Date.parse(incident.reading.observed_at));
  const copying = await evidence('pl_inventory_items');
  assert.equal(copying.delivery.state, 'absent');
  assert.equal(copying.delivery.observed_at, null);
  const paused = await evidence('pl_inventory_movements');
  assert.equal(paused.reading.state, 'absent');
  const never = await evidence('pl_sales_archive');
  assert.equal(never.evidence_kind, 'absent');
  assert.equal(never.collected_at, null);
  for (const body of [incident, copying, paused, never]) {
    assert.equal(body.reconciliation.state, 'absent');
    assert.notEqual(body.evidence_kind, 'live');
  }
});

test('a table paused in the demo stops reporting lag and throughput', async () => {
  const fetch = createCockpitDemoFetch();
  const before = await (await fetch('/v2/pipelines/pl_sales_orders/metrics?window=1h')).json();
  assert.ok(before.points.every((point: { lag_seconds: number | null }) => point.lag_seconds !== null));
  await fetch('/v2/pipelines/pl_sales_orders/actions/pause', { method: 'POST', body: JSON.stringify({}) });
  const after = await (await fetch('/v2/pipelines/pl_sales_orders/metrics?window=1h')).json();
  assert.ok(after.points.every((point: { lag_seconds: number | null; throughput_rows_per_second: number | null }) =>
    point.lag_seconds === null && point.throughput_rows_per_second === null));
});

test('table suspension leaves shared capture unconfirmed and preserves other table evidence', async () => {
  const fetch = createCockpitDemoFetch();
  const evidence = async (id: string) => (await fetch(`/v2/pipelines/${id}/evidence`)).json();
  const before = await evidence('pl_sales_orders');
  const other = await evidence('pl_sales_customers');
  await fetch('/v2/pipelines/pl_sales_orders/actions/pause', {
    method: 'POST', body: JSON.stringify({ dry_run: true }),
  });
  assert.deepEqual(await evidence('pl_sales_orders'), before);
  await fetch('/v2/pipelines/pl_sales_orders/actions/pause', {
    method: 'POST', body: JSON.stringify({}),
  });
  const paused = await evidence('pl_sales_orders');
  assert.equal(paused.evidence_kind, 'simulation');
  assert.equal(paused.reading.state, 'absent');
  for (const field of ['observed_at', 'event_count', 'lag_seconds', 'duplicate_count']) {
    assert.equal(paused.reading[field], null);
  }
  assert.match(paused.reading.detail, /cannot be inferred from table suspension/);
  assert.equal(paused.delivery.state, 'stale');
  assert.match(paused.delivery.detail, /synthetic delivery/);
  assert.deepEqual(await evidence('pl_sales_customers'), other);
  const list = await (await fetch('/v2/pipelines')).json();
  const row = list.items.find((item: { id: string }) => item.id === 'pl_sales_orders');
  assert.match(row.absent_reasons.lag_seconds, /delivery suspended.*not measured/i);
  assert.doesNotMatch(JSON.stringify(row.absent_reasons), /no reading in progress/);
  await fetch('/v2/pipelines/pl_sales_orders/actions/resume', {
    method: 'POST', body: JSON.stringify({}),
  });
  assert.deepEqual(await evidence('pl_sales_orders'), before);
});

const json = async (response: Response) => response.json() as Promise<Record<string, any>>;

test('the demo serves a synthetic control-plane diagnostic that never passes capture, delivery or reconciliation', async () => {
  const fetch = createCockpitDemoFetch();
  const report = await json(await fetch('/v2/diagnostics/report'));
  assert.equal(report.schema, 'quadringent.diagnostic.v1');
  assert.equal(report.scope, 'control_plane');
  assert.equal(report.synthetic, true);
  const results = report.results as Record<string, any>[];
  assert.ok(results.length > 10);
  for (const result of results) {
    if (['capture.reading', 'delivery.snowflake', 'reconciliation.destination', 'evidence.freshness'].includes(result.code)) {
      assert.notEqual(result.status, 'passed', `${result.code} ${result.subject?.ref}`);
    }
    if (result.evidence?.evidence_kind !== undefined) assert.notEqual(result.evidence.evidence_kind, 'live');
  }
  assert.ok(results.some((result) => result.status === 'failed'), 'the demo incident is visible');
  assert.ok(results.some((result) => result.status === 'unknown'));
  assert.equal(report.overall.status, 'blocked');
  assert.match(report.overall.summary, /Synthetic data/);
});

test('the demo diagnostic parses with the strict client parser and keeps the stale delivery date', async () => {
  const report = await client().getDiagnostics();
  const delivery = report.results.find((result) => result.code === 'delivery.snowflake' && result.subject?.ref === 'pl_sales_order_lines')!;
  assert.equal(delivery.status, 'failed');
  assert.equal(delivery.since, '2026-09-23T08:40:00Z');
  assert.equal(delivery.evidenceKind, 'simulation');
});

test('the pipeline filter keeps one table and 404s on an unknown pipeline', async () => {
  const c = client();
  const report = await c.getDiagnostics({ pipelineId: 'pl_sales_orders' });
  const refs = new Set(report.results.filter((result) => result.subject?.kind === 'pipeline').map((result) => result.subject!.ref));
  assert.deepEqual([...refs], ['pl_sales_orders']);
  assert.ok(report.results.some((result) => result.code === 'control_plane.api'));
  await assert.rejects(() => c.getDiagnostics({ pipelineId: 'pl_unknown' }), (error: unknown) => (error as { status: number }).status === 404);
});

test('the demo export matches its own preview and says it is synthetic', async () => {
  const exported = await client().getDiagnosticsExport();
  assert.deepEqual(Object.keys(exported.files).sort(), exported.preview.files.map((file) => file.name).sort());
  const { checkExportIntegrity } = await import('../../domain/diagnosticsExport.ts');
  const integrity = await checkExportIntegrity(exported);
  assert.deepEqual(integrity.problems, []);
  assert.ok(exported.preview.limits.some((limit) => /Synthetic data/.test(limit)));
});

test('source and fleet actions report the tables they changed, as the real service does', async () => {
  const c = client();
  const envelope = await c.runSourceAction(DEMO_SOURCE_INVENTORY, 'pause');
  const applied = (envelope.after as { pipelines: { applied: { pipeline_id: string; after: { declared_state: string } }[] } }).pipelines.applied;
  assert.ok(applied.length > 0);
  assert.ok(applied.every((item) => item.after.declared_state === 'paused'));
  for (const item of applied) assert.equal((await c.getPipeline(item.pipeline_id)).declaredState, 'paused');
});

test('a pipeline preview announces the transition it would make, without applying it', async () => {
  const c = client();
  const envelope = await c.runPipelineAction('pl_sales_customers', 'pause', { dryRun: true });
  assert.deepEqual((envelope.dryRun as { would_transition: unknown }).would_transition, { from: 'live', to: 'paused' });
  assert.equal((await c.getPipeline('pl_sales_customers')).declaredState, 'live');
});
