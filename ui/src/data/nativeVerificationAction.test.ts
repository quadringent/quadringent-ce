import assert from 'node:assert/strict';
import test from 'node:test';
import { runNativeVerification } from './nativeVerificationAction.ts';
import { nativeRecordFixture } from './nativeVerification.testFixtures.ts';
import { parseNativeVerification } from './nativeVerification.ts';

test('execution is followed by an exact receipt read-back and capability recheck', async () => {
  const calls: string[] = [];
  const record = parseNativeVerification(nativeRecordFixture(), 'pl_fixture')!;
  const client = { getNativeCapabilities: async () => { calls.push('capability'); return { available: true }; },
    requestNativeVerification: async () => { calls.push('execute'); return record; },
    getNativeVerification: async () => { calls.push('readback'); return record; } };
  assert.equal((await runNativeVerification(client as never, 'pl_fixture')).id, record.id);
  assert.deepEqual(calls, ['capability', 'execute', 'readback']);
});

test('unavailable capability and changed or superseded read-back never confirm an action', async () => {
  const record = parseNativeVerification(nativeRecordFixture(), 'pl_fixture')!;
  for (const defect of ['unavailable', 'superseded', 'other', 'context']) {
    let writes = 0;
    const client = { getNativeCapabilities: async () => ({ available: defect !== 'unavailable' }),
      requestNativeVerification: async () => { writes++; return record; },
      getNativeVerification: async () => ({ ...record, id: defect === 'other' ? 'other' : record.id,
        contextSha256: defect === 'context' ? 'b'.repeat(64) : record.contextSha256,
        contextValidity: defect === 'superseded' ? 'superseded' : 'same_context' }) };
    await assert.rejects(() => runNativeVerification(client as never, 'pl_fixture'));
    assert.equal(writes, defect === 'unavailable' ? 0 : 1);
  }
});
