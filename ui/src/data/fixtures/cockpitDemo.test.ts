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
  const after = await c.getPipeline('pl_inventory_warehouses');
  assert.equal(after.declaredState, before.declaredState);
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

test('costs cover the three provenances: measured (connection), estimated, and absent (paused table)', async () => {
  const c = client();
  const measuredConnection = await c.getCosts('connection', DEMO_SOURCE_SALES, '24h');
  assert.equal(measuredConnection.status, 'measured');
  const estimatedConnection = await c.getCosts('connection', DEMO_SOURCE_INVENTORY, '24h');
  assert.equal(estimatedConnection.status, 'estimated');
  const absentTable = await c.getCosts('table', 'pl_inventory_movements', '24h');
  assert.equal(absentTable.status, 'absent');
  assert.equal(absentTable.amount, null);
});
test('table evidence remains explicitly synthetic and never claims complete reconciliation', async () => {
  const fetch = createCockpitDemoFetch();
  const body = await (await fetch('/v2/pipelines/pl_sales_orders/evidence')).json();
  assert.equal(body.evidence_kind, 'simulation');
  assert.equal(body.reconciliation.state, 'absent');
  assert.equal(body.reconciliation.duplicate_count, null);
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
  assert.equal(paused.reading.state, 'stale');
  const never = await evidence('pl_sales_archive');
  assert.equal(never.evidence_kind, 'absent');
  assert.equal(never.collected_at, null);
  for (const body of [incident, copying, paused, never]) {
    assert.equal(body.reconciliation.state, 'absent');
    assert.notEqual(body.evidence_kind, 'live');
  }
});
