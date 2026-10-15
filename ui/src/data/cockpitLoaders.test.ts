import assert from 'node:assert/strict';
import test from 'node:test';
import { runCancellable, type LoadOutcome } from './cancellableLoad.ts';
import { loadHomeRows, loadTableData, tableDataMatches, type CockpitTableData } from './cockpitLoaders.ts';
import type { ControlPlaneV2Client } from './controlPlaneV2Client.ts';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
const emptyStage = { state: 'absent', observedAt: null, eventCount: null, lagSeconds: null, duplicateCount: null, detail: '' } as const;

/** A fake client whose reads for `slowId` wait on a gate; the others answer at once. */
function fakeClient(slowId: string, gate: Promise<void>, seenSignals: AbortSignal[] = []): ControlPlaneV2Client {
  const wait = async (id: string, signal?: AbortSignal) => {
    if (signal) seenSignals.push(signal);
    if (id === slowId) await gate;
  };
  return {
    getPipeline: async (id: string, signal?: AbortSignal) => { await wait(id, signal); return { id, declaredState: id === slowId ? 'attention' : 'live' }; },
    getMetrics: async (id: string, window: '1h' | '24h', signal?: AbortSignal) => { await wait(id, signal); return { window, points: [] }; },
    getLogs: async (id: string, _filters: unknown, signal?: AbortSignal) => { await wait(id, signal); return []; },
    getCosts: async () => null,
    listPipelines: async () => [],
    getEvidence: async (id: string) => ({ pipelineId: id, provenance: 'p', evidenceKind: 'live', collectedAt: null, reading: emptyStage, delivery: emptyStage, reconciliation: emptyStage }),
  } as unknown as ControlPlaneV2Client;
}

const resolvers = async () => ({ sourceIdForPipeline: () => null, nameForPipeline: (id: string) => id });

test('table selection race: a slow pipeline A answering after B never replaces B', async () => {
  const gate = deferred<void>();
  const client = fakeClient('pl_a', gate.promise);
  const shown: string[] = [];
  const show = (outcome: LoadOutcome<CockpitTableData>) => {
    if (outcome.status === 'ready') shown.push(outcome.value.pipeline.id);
  };

  const cancelA = runCancellable((signal) => loadTableData(client, resolvers, 'pl_a', '1h', {}, signal), show);
  cancelA(); // the selection moved to B
  runCancellable((signal) => loadTableData(client, resolvers, 'pl_b', '1h', {}, signal), show);

  await tick();
  gate.resolve(); // A finally answers
  await tick();

  assert.deepEqual(shown, ['pl_b']);
});

test('without a newer selection the slow answer is still shown', async () => {
  const gate = deferred<void>();
  const client = fakeClient('pl_a', gate.promise);
  const shown: string[] = [];
  runCancellable((signal) => loadTableData(client, resolvers, 'pl_a', '1h', {}, signal), (o) => { if (o.status === 'ready') shown.push(o.value.pipeline.id); });
  gate.resolve();
  await tick();
  assert.deepEqual(shown, ['pl_a']);
});

test('every table request carries the abort signal of its selection', async () => {
  const gate = deferred<void>();
  const signals: AbortSignal[] = [];
  const client = fakeClient('pl_a', gate.promise, signals);
  const cancel = runCancellable((signal) => loadTableData(client, resolvers, 'pl_a', '24h', { level: 'error' }, signal), () => {});
  await tick();
  assert.equal(signals.length, 3);
  assert.ok(signals.every((signal) => signal.aborted === false));
  cancel();
  assert.ok(signals.every((signal) => signal.aborted === true));
  gate.resolve();
});

test('home rows pass the signal to every metrics read', async () => {
  const signals: AbortSignal[] = [];
  const client = {
    getMetrics: async (_id: string, _window: string, signal?: AbortSignal) => { if (signal) signals.push(signal); return { window: '1h', points: [] }; },
    getCosts: async () => null,
  } as unknown as ControlPlaneV2Client;
  const controller = new AbortController();
  const rows = await loadHomeRows(client, [{ id: 'src_1', label: 'Sales', destinationId: null, tables: [
    { pipelineId: 'pl_1' }, { pipelineId: 'pl_2' },
  ] as never }], controller.signal);
  assert.equal(rows.length, 1);
  assert.equal(signals.length, 2);
  assert.ok(signals.every((signal) => signal === controller.signal));
});

test('data loaded for another table does not match the current selection', async () => {
  const gate = deferred<void>();
  gate.resolve();
  const data = await loadTableData(fakeClient('none', gate.promise), resolvers, 'pl_a', '1h', {});
  assert.equal(tableDataMatches(data, 'pl_a'), true);
  assert.equal(tableDataMatches(data, 'pl_b'), false);
});
