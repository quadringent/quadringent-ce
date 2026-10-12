import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlPlaneV2Client } from '../../data/controlPlaneV2Client.ts';
import { EffectNotConfirmedError } from '../../domain/controlsPanel.ts';
import { scopeControls, tableControls } from './cockpitActions.ts';

interface Recorded { readonly method: string; readonly path: string; readonly body: Record<string, unknown> }

/** A fake service: `states` is the live declared state per pipeline. */
function fakeService(options: {
  readonly states: Record<string, string>;
  readonly onAction?: (path: string, body: Record<string, unknown>) => Response | null;
  readonly readFails?: boolean;
}) {
  const log: Recorded[] = [];
  const client = new ControlPlaneV2Client({
    fetchFn: async (input, init) => {
      const method = (init?.method ?? 'GET').toUpperCase();
      const path = input.replace(/^\/v2/, '');
      const body = init?.body ? JSON.parse(init.body as string) as Record<string, unknown> : {};
      log.push({ method, path, body });
      const json = (status: number, payload: unknown) => new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
      if (method === 'POST') {
        const custom = options.onAction?.(path, body);
        if (custom) return custom;
        return json(500, { error: { code: 'internal_error', message: 'unexpected', next_action: '', retryable: false } });
      }
      const match = /^\/pipelines\/([^/]+)$/.exec(path);
      if (match) {
        if (options.readFails) return json(500, { error: { code: 'internal_error', message: 'read failed', next_action: '', retryable: true } });
        const state = options.states[match[1]!];
        return state ? json(200, { id: match[1], declared_state: state }) : json(404, { error: { code: 'not_found', message: 'gone', next_action: '', retryable: false } });
      }
      return json(404, { error: { code: 'not_found', message: path, next_action: '', retryable: false } });
    },
  });
  return { client, log, getClient: async () => client };
}

const accepted = (state: string) => (): Response => new Response(JSON.stringify({
  before: { id: 'pl_1', declared_state: 'live' }, after: { id: 'pl_1', declared_state: state }, verify: { method: 'GET', path: '/v2/pipelines/pl_1' }, dry_run: null,
}), { status: 200, headers: { 'Content-Type': 'application/json' } });

const signal = new AbortController().signal;

test('table execute returns the accepted state and the confirmation id of a sensitive action', async () => {
  const { getClient } = fakeService({
    states: { pl_1: 'paused' },
    onAction: (_path, body) => body.confirmation_token === 'cf_1'
      ? accepted('stopped')()
      : new Response(JSON.stringify({ error: { code: 'pending_confirmation_required', message: 'confirmation required (id=cf_1) — see /v2/confirmations/cf_1', next_action: '', retryable: false } }), { status: 409, headers: { 'Content-Type': 'application/json' } }),
  });
  const controls = tableControls('pl_1', { getClient, onSettled() {} });
  assert.deepEqual(await controls.execute('remove', undefined, signal), { pendingConfirmationId: 'cf_1' });
  const done = await controls.execute('remove', 'cf_1', signal);
  assert.equal(done.pendingConfirmationId, null);
  assert.deepEqual(done.accepted, { id: 'pl_1', declaredState: 'stopped' });
});

test('table verify confirms only when a fresh read shows the accepted state', async () => {
  const settled: string[] = [];
  const service = fakeService({ states: { pl_1: 'paused' } });
  const controls = tableControls('pl_1', { getClient: service.getClient, onSettled: () => settled.push('settled') });
  await controls.verify({ action: 'pause', accepted: { id: 'pl_1', declaredState: 'paused' }, preview: null, signal });
  assert.deepEqual(service.log.map((entry) => `${entry.method} ${entry.path}`), ['GET /pipelines/pl_1']);
  assert.deepEqual(settled, ['settled']);
});

test('table verify rejects a state that differs, naming both states', async () => {
  const settled: string[] = [];
  const service = fakeService({ states: { pl_1: 'live' } });
  const controls = tableControls('pl_1', { getClient: service.getClient, onSettled: () => settled.push('settled') });
  await assert.rejects(
    () => controls.verify({ action: 'pause', accepted: { declaredState: 'paused' }, preview: null, signal }),
    (error: unknown) => error instanceof EffectNotConfirmedError && /reports "live" while "paused" was expected/.test(error.message),
  );
  assert.deepEqual(settled, ['settled'], 'the screen still refreshes to show the real state');
});

test('table verify uses the announced effect after an unknown result and refuses to compare without a reference', async () => {
  const service = fakeService({ states: { pl_1: 'paused' } });
  const controls = tableControls('pl_1', { getClient: service.getClient, onSettled() {} });
  await controls.verify({ action: 'pause', accepted: null, preview: { dryRun: { would_transition: { from: 'live', to: 'paused' } } }, signal });
  await assert.rejects(
    () => controls.verify({ action: 'pause', accepted: null, preview: { dryRun: { action: 'pause' } }, signal }),
    /did not report what to expect/,
  );
});

test('a read-back that fails is an error, not a confirmation', async () => {
  const service = fakeService({ states: { pl_1: 'paused' }, readFails: true });
  const controls = tableControls('pl_1', { getClient: service.getClient, onSettled() {} });
  await assert.rejects(() => controls.verify({ action: 'pause', accepted: { declaredState: 'paused' }, preview: null, signal }), /read failed/);
});

test('a replay cannot be confirmed by the declared state and says so', async () => {
  const service = fakeService({ states: { pl_1: 'live' } });
  const controls = tableControls('pl_1', { getClient: service.getClient, onSettled() {} });
  await assert.rejects(
    () => controls.verify({ action: 'replay', accepted: { declaredState: 'live' }, preview: null, signal }),
    (error: unknown) => error instanceof EffectNotConfirmedError && /Evidence tab/.test(error.message),
  );
});

test('a removed table that is gone on the read-back counts as confirmed', async () => {
  const service = fakeService({ states: {} });
  const controls = tableControls('pl_1', { getClient: service.getClient, onSettled() {} });
  await controls.verify({ action: 'remove', accepted: { declaredState: 'stopped' }, preview: null, signal });
  await assert.rejects(() => controls.verify({ action: 'pause', accepted: { declaredState: 'paused' }, preview: null, signal }), /gone/);
});

test('every table request passes the abort signal and the dry run never mutates', async () => {
  const service = fakeService({ states: { pl_1: 'live' }, onAction: (_p, body) => body.dry_run === true
    ? new Response(JSON.stringify({ before: null, after: null, verify: null, dry_run: { would_transition: { from: 'live', to: 'paused' } } }), { status: 200 })
    : null });
  const controls = tableControls('pl_1', { getClient: service.getClient, onSettled() {} });
  const preview = await controls.dryRun('pause', signal) as { dryRun: unknown };
  assert.deepEqual(preview.dryRun, { would_transition: { from: 'live', to: 'paused' } });
  assert.equal(service.log.length, 1);
  assert.equal(service.log[0]!.body.dry_run, true);
});

const scopeAccepted = (applied: readonly [string, string][]) => (): Response => new Response(JSON.stringify({
  before: null,
  after: { pipelines: { applied: applied.map(([id, state]) => ({ pipeline_id: id, before: { declared_state: 'live' }, after: { declared_state: state } })), skipped: [] } },
  verify: { method: 'GET', path: '/v2/sources' }, dry_run: null,
}), { status: 200, headers: { 'Content-Type': 'application/json' } });

test('a connection action is verified table by table against what the service reported', async () => {
  const service = fakeService({ states: { pl_1: 'paused', pl_2: 'paused' }, onAction: scopeAccepted([['pl_1', 'paused'], ['pl_2', 'paused']]) });
  const controls = scopeControls({ level: 'connection', id: 'src_1' }, { getClient: service.getClient, onSettled() {} });
  const result = await controls.execute('pause', undefined, signal);
  await controls.verify({ action: 'pause', accepted: result.accepted, preview: null, signal });
  assert.deepEqual(service.log.filter((entry) => entry.method === 'GET').map((entry) => entry.path).sort(), ['/pipelines/pl_1', '/pipelines/pl_2']);
});

test('a connection action with one table not in the reported state is not confirmed', async () => {
  const service = fakeService({ states: { pl_1: 'paused', pl_2: 'live' }, onAction: scopeAccepted([['pl_1', 'paused'], ['pl_2', 'paused']]) });
  const controls = scopeControls({ level: 'connection', id: 'src_1' }, { getClient: service.getClient, onSettled() {} });
  const result = await controls.execute('pause', undefined, signal);
  await assert.rejects(
    () => controls.verify({ action: 'pause', accepted: result.accepted, preview: null, signal }),
    (error: unknown) => error instanceof EffectNotConfirmedError && /1 of 2 tables/.test(error.message),
  );
});

test('a scope action whose answer does not say what changed cannot be confirmed', async () => {
  const service = fakeService({ states: {}, onAction: () => new Response(JSON.stringify({ before: null, after: { affected: ['pl_1'] }, verify: null, dry_run: null }), { status: 200 }) });
  const controls = scopeControls({ level: 'destination', id: 'dst_1' }, { getClient: service.getClient, onSettled() {} });
  const result = await controls.execute('resume', undefined, signal);
  await assert.rejects(() => controls.verify({ action: 'resume', accepted: result.accepted, preview: null, signal }), /did not report what to expect/);
});

test('a fleet action asks for the confirmation the service requires instead of failing', async () => {
  const service = fakeService({ states: {}, onAction: (path, body) => {
    assert.equal(path, '/actions/pause_all');
    return body.confirmation_token
      ? scopeAccepted([])()
      : new Response(JSON.stringify({ error: { code: 'pending_confirmation_required', message: 'confirmation required (id=cf_9) — see /v2/confirmations/cf_9', next_action: '', retryable: false } }), { status: 409 });
  } });
  const controls = scopeControls({ level: 'fleet' }, { getClient: service.getClient, onSettled() {} });
  assert.deepEqual(await controls.execute('pause', undefined, signal), { pendingConfirmationId: 'cf_9' });
  const done = await controls.execute('pause', 'cf_9', signal);
  assert.equal(done.pendingConfirmationId, null);
  assert.ok(controls.approveConfirmation && controls.rejectConfirmation);
});

test('a scope level cannot run a sensitive table action', async () => {
  const service = fakeService({ states: {} });
  const controls = scopeControls({ level: 'connection', id: 'src_1' }, { getClient: service.getClient, onSettled() {} });
  await assert.rejects(() => controls.execute('remove', undefined, signal), /not supported at connection level/);
  assert.equal(service.log.length, 0);
});
