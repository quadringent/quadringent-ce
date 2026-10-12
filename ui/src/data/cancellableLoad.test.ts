import assert from 'node:assert/strict';
import test from 'node:test';
import { runCancellable, type LoadOutcome } from './cancellableLoad.ts';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

test('a slow response for the previous selection is never delivered after the next one', async () => {
  const slow = deferred<string>();
  const fast = deferred<string>();
  const delivered: LoadOutcome<string>[] = [];

  const cancelA = runCancellable(() => slow.promise, (outcome) => delivered.push(outcome));
  // The selection changes: React runs the cleanup of effect A, then effect B.
  cancelA();
  runCancellable(() => fast.promise, (outcome) => delivered.push(outcome));

  fast.resolve('B');
  await tick();
  slow.resolve('A');
  await tick();

  assert.deepEqual(delivered, [{ status: 'ready', value: 'B' }]);
});

test('cancelling aborts the request signal given to the loader', async () => {
  let seen: AbortSignal | null = null;
  const cancel = runCancellable((signal) => { seen = signal; return new Promise<string>(() => {}); }, () => {});
  assert.equal((seen as AbortSignal | null)?.aborted, false);
  cancel();
  assert.equal((seen as AbortSignal | null)?.aborted, true);
});

test('an error that arrives after cancellation is ignored, including the abort error itself', async () => {
  const pending = deferred<string>();
  const delivered: LoadOutcome<string>[] = [];
  const cancel = runCancellable(() => pending.promise, (outcome) => delivered.push(outcome));
  cancel();
  pending.reject(new DOMException('aborted', 'AbortError'));
  await tick();
  assert.deepEqual(delivered, []);
});

test('a current failure is delivered with its message', async () => {
  const delivered: LoadOutcome<string>[] = [];
  runCancellable(async () => { throw new Error('service down'); }, (outcome) => delivered.push(outcome));
  await tick();
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0]!.status, 'failed');
  assert.equal(delivered[0]!.status === 'failed' ? delivered[0]!.message : '', 'service down');
});

test('each call owns its own flag: cancelling one never silences another', async () => {
  const first = deferred<string>();
  const second = deferred<string>();
  const delivered: string[] = [];
  const cancelFirst = runCancellable(() => first.promise, (o) => { if (o.status === 'ready') delivered.push(o.value); });
  runCancellable(() => second.promise, (o) => { if (o.status === 'ready') delivered.push(o.value); });
  cancelFirst();
  first.resolve('first');
  second.resolve('second');
  await tick();
  assert.deepEqual(delivered, ['second']);
});
