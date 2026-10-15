import assert from 'node:assert/strict';
import test from 'node:test';
import { EffectNotConfirmedError, type ControlsPanelState } from '../domain/controlsPanel.ts';
import { createControlsFlow, runWithTimeout, type FlowDeps } from './controlsPanelFlow.ts';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const tick = (ms = 0) => new Promise<void>((resolve) => setTimeout(resolve, ms));

interface Harness {
  readonly flow: ReturnType<typeof createControlsFlow>;
  readonly states: ControlsPanelState[];
  readonly calls: string[];
  readonly last: () => ControlsPanelState;
}

function harness(overrides: Partial<FlowDeps> = {}): Harness {
  const calls: string[] = [];
  const states: ControlsPanelState[] = [];
  const deps: FlowDeps = {
    timeoutMs: 1000,
    dryRun: async (action) => { calls.push(`dryRun:${action}`); return { dryRun: { would_transition: { from: 'live', to: 'paused' } } }; },
    execute: async (action, token) => { calls.push(`execute:${action}:${token ?? ''}`); return { pendingConfirmationId: null, accepted: { declaredState: 'paused' } }; },
    verify: async () => { calls.push('verify'); },
    approveConfirmation: async (id) => { calls.push(`approve:${id}`); },
    rejectConfirmation: async (id) => { calls.push(`reject:${id}`); },
    ...overrides,
  };
  const flow = createControlsFlow(() => deps, (state) => states.push(state));
  flow.attach();
  return { flow, states, calls, last: () => states.at(-1)! };
}

async function readyToConfirm(h: Harness, action: 'pause' | 'remove' = 'pause') {
  h.flow.request(action);
  await tick();
  assert.equal(h.last().phase, 'preview_ready');
}

test('the happy path previews, executes, reads again and only then reports success', async () => {
  const h = harness();
  await readyToConfirm(h);
  h.flow.confirm();
  assert.equal(h.last().phase, 'running');
  await tick();
  assert.deepEqual(h.calls, ['dryRun:pause', 'execute:pause:', 'verify']);
  const phases = h.states.map((state) => state.phase);
  assert.ok(phases.indexOf('verifying') < phases.indexOf('succeeded'), phases.join('>'));
  assert.equal(h.last().phase, 'succeeded');
});

test('a double click on Confirm sends a single mutation request', async () => {
  const h = harness();
  await readyToConfirm(h);
  h.flow.confirm();
  h.flow.confirm();
  h.flow.confirm();
  await tick();
  assert.equal(h.calls.filter((call) => call.startsWith('execute')).length, 1);
});

test('a double click on an action button sends a single preview request and a click while busy is ignored', async () => {
  const gate = deferred<unknown>();
  const h = harness({ dryRun: async (action) => { h.calls.push(`dryRun:${action}`); return gate.promise; } });
  h.flow.request('pause');
  h.flow.request('pause');
  h.flow.request('resume');
  assert.deepEqual(h.calls, ['dryRun:pause']);
  gate.resolve({});
  await tick();
  assert.equal(h.last().action, 'pause');
});

test('the accepted request is not confirmed while the read-back is still pending', async () => {
  const gate = deferred<void>();
  const h = harness({ verify: async () => gate.promise });
  await readyToConfirm(h);
  h.flow.confirm();
  await tick();
  assert.equal(h.last().phase, 'verifying');
  assert.notEqual(h.last().phase, 'succeeded');
  gate.resolve();
  await tick();
  assert.equal(h.last().phase, 'succeeded');
});

test('a failed read-back reads "effect not confirmed", never success', async () => {
  const h = harness({ verify: async () => { throw new Error('network down'); } });
  await readyToConfirm(h);
  h.flow.confirm();
  await tick();
  assert.equal(h.last().phase, 'failed');
  assert.equal(h.last().failure, 'not_confirmed');
  assert.match(h.last().error ?? '', /^Effect not confirmed\./);
  assert.ok(h.states.every((state) => state.phase !== 'succeeded'));
});

test('a read-back that shows another state is not confirmed and says which state was seen', async () => {
  const h = harness({ verify: async () => { throw new EffectNotConfirmedError('The service reports "live" while "paused" was expected.'); } });
  await readyToConfirm(h);
  h.flow.confirm();
  await tick();
  assert.equal(h.last().failure, 'not_confirmed');
  assert.match(h.last().error ?? '', /reports "live" while "paused" was expected/);
});

test('a timeout is an unknown result, aborts the request and never sends a second mutation', async () => {
  let seenSignal: AbortSignal | null = null;
  const h = harness({
    timeoutMs: 15,
    execute: async (action, _token, signal) => { h.calls.push(`execute:${action}`); seenSignal = signal; return new Promise(() => {}); },
  });
  await readyToConfirm(h);
  h.flow.confirm();
  await tick(60);
  assert.equal(h.last().phase, 'failed');
  assert.equal(h.last().failure, 'unknown_result');
  assert.match(h.last().error ?? '', /Result unknown\./);
  assert.match(h.last().error ?? '', /Check the current state before retrying/);
  assert.equal((seenSignal as AbortSignal | null)?.aborted, true);
  await tick(60);
  assert.equal(h.calls.filter((call) => call.startsWith('execute')).length, 1, 'no automatic retry');
});

test('a late success after the timeout is ignored', async () => {
  const gate = deferred<{ pendingConfirmationId: string | null; accepted?: unknown }>();
  const h = harness({ timeoutMs: 10, execute: async () => gate.promise });
  await readyToConfirm(h);
  h.flow.confirm();
  await tick(40);
  assert.equal(h.last().failure, 'unknown_result');
  gate.resolve({ pendingConfirmationId: null, accepted: { declaredState: 'paused' } });
  await tick(20);
  assert.equal(h.last().failure, 'unknown_result');
  assert.ok(h.states.every((state) => state.phase !== 'succeeded'));
  assert.ok(!h.calls.includes('verify'));
});

test('after an unknown result, "check state" reads again without any mutation', async () => {
  const h = harness({ timeoutMs: 10, execute: async () => { h.calls.push('execute'); return new Promise(() => {}); } });
  await readyToConfirm(h);
  h.flow.confirm();
  await tick(40);
  assert.equal(h.last().failure, 'unknown_result');
  h.flow.checkState();
  await tick(20);
  assert.equal(h.calls.filter((call) => call === 'execute').length, 1);
  assert.deepEqual(h.calls.filter((call) => call === 'verify'), ['verify']);
  assert.equal(h.last().phase, 'succeeded');
});

test('a transport failure on the mutation is an unknown result', async () => {
  const h = harness({ execute: async () => { throw Object.assign(new Error('Failed'), { status: 0 }); } });
  await readyToConfirm(h);
  h.flow.confirm();
  await tick();
  assert.equal(h.last().failure, 'unknown_result');
  assert.match(h.last().error ?? '', /may have been applied/);
});

test('a refused action (403 / 409) is explained and reads as nothing changed', async () => {
  for (const [status, code, pattern] of [
    [403, 'insufficient_role', /role does not allow/],
    [409, 'capability_unavailable', /not possible in the current state/],
  ] as const) {
    const h = harness({ execute: async () => { throw Object.assign(new Error('refused by service'), { status, code }); } });
    await readyToConfirm(h);
    h.flow.confirm();
    await tick();
    assert.equal(h.last().phase, 'failed');
    assert.equal(h.last().failure, 'refused');
    assert.match(h.last().error ?? '', pattern);
    assert.ok(!h.calls.includes('verify'), 'nothing to read back after a refusal');
  }
});

test('a sensitive action waits for approval, then the explicit rerun carries the confirmation id once', async () => {
  let attempt = 0;
  const h = harness({
    execute: async (action, token) => {
      h.calls.push(`execute:${action}:${token ?? ''}`);
      attempt += 1;
      return attempt === 1 ? { pendingConfirmationId: 'cf_1' } : { pendingConfirmationId: null, accepted: { declaredState: 'stopped' } };
    },
  });
  await readyToConfirm(h, 'remove');
  h.flow.confirm();
  await tick();
  assert.equal(h.last().phase, 'awaiting_confirmation');
  h.flow.confirm(); // not approved yet: no second request
  assert.equal(h.calls.filter((call) => call.startsWith('execute')).length, 1);
  h.flow.approve();
  h.flow.approve();
  await tick();
  assert.equal(h.calls.filter((call) => call.startsWith('approve')).length, 1, 'double approval is one request');
  assert.equal(h.last().confirmationState, 'approved');
  h.flow.confirm('cf_1');
  h.flow.confirm('cf_1');
  await tick();
  assert.deepEqual(h.calls.filter((call) => call.startsWith('execute')), ['execute:remove:', 'execute:remove:cf_1']);
  assert.equal(h.last().phase, 'succeeded');
});

test('a failed approval is explained and the confirmation stays pending', async () => {
  const h = harness({
    execute: async () => ({ pendingConfirmationId: 'cf_1' }),
    approveConfirmation: async () => { throw Object.assign(new Error('no'), { status: 403, code: 'insufficient_role' }); },
  });
  await readyToConfirm(h, 'remove');
  h.flow.confirm();
  await tick();
  h.flow.approve();
  await tick();
  assert.equal(h.last().phase, 'awaiting_confirmation');
  assert.equal(h.last().confirmationState, 'pending');
  assert.match(h.last().error ?? '', /role does not allow/);
});

test('verification receives the accepted answer and the preview of the same action', async () => {
  let context: unknown = null;
  const h = harness({ verify: async (ctx) => { context = ctx; } });
  await readyToConfirm(h);
  h.flow.confirm();
  await tick();
  const seen = context as { action: string; accepted: unknown; preview: unknown };
  assert.equal(seen.action, 'pause');
  assert.deepEqual(seen.accepted, { declaredState: 'paused' });
  assert.deepEqual(seen.preview, { dryRun: { would_transition: { from: 'live', to: 'paused' } } });
});

test('results arriving after the panel is detached never reach the screen', async () => {
  const gate = deferred<{ pendingConfirmationId: string | null }>();
  const h = harness({ execute: async () => gate.promise });
  await readyToConfirm(h);
  h.flow.confirm();
  const before = h.states.length;
  h.flow.detach();
  gate.resolve({ pendingConfirmationId: null });
  await tick(20);
  assert.equal(h.states.length, before);
  assert.ok(!h.calls.includes('verify'));
});

test('a failed preview never reaches the mutation', async () => {
  const h = harness({ dryRun: async () => { throw new Error('preview unavailable'); } });
  h.flow.request('pause');
  await tick();
  assert.equal(h.last().phase, 'failed');
  h.flow.confirm();
  await tick();
  assert.ok(!h.calls.some((call) => call.startsWith('execute')));
});

test('runWithTimeout resolves with the value, rejects on time-out and aborts its signal', async () => {
  assert.equal(await runWithTimeout(async () => 'ok', 50), 'ok');
  let signal: AbortSignal | null = null;
  await assert.rejects(
    () => runWithTimeout((s) => { signal = s; return new Promise(() => {}); }, 10),
    (error: unknown) => (error as Error).name === 'ActionTimeoutError',
  );
  assert.equal((signal as AbortSignal | null)?.aborted, true);
});

test('what the read-back confirmed is kept for the success message, and nothing is invented without it', async () => {
  const withDetail = harness({ verify: async () => 'paused' });
  await readyToConfirm(withDetail);
  withDetail.flow.confirm();
  await tick();
  assert.equal(withDetail.last().phase, 'succeeded');
  assert.equal(withDetail.last().confirmed, 'paused');
  const bare = harness();
  await readyToConfirm(bare);
  bare.flow.confirm();
  await tick();
  assert.equal(bare.last().confirmed, null);
});
