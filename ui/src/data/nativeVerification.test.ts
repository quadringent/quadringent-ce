import assert from 'node:assert/strict';
import test from 'node:test';
import { parseNativeCapabilities, parseNativeVerification } from './nativeVerification.ts';
import { nativeRecordFixture } from './nativeVerification.testFixtures.ts';
import { ControlPlaneV2Client } from './controlPlaneV2Client.ts';

test('native capabilities require exact availability and a known mode', () => {
  assert.deepEqual(parseNativeCapabilities({ available: true, mode: 'observation', reason_code: null }),
    { available: true, mode: 'observation', reasonCode: null });
  for (const value of [{ available: 'true', mode: 'observation', reason_code: null },
    { available: true, mode: null, reason_code: null }, { available: true, mode: 'future', reason_code: null }]) {
    assert.throws(() => parseNativeCapabilities(value));
  }
});

test('observed equality remains incomplete and exposes seven sealed datasets separately from admission', () => {
  const record = parseNativeVerification(nativeRecordFixture(), 'pl_fixture')!;
  assert.equal(record.outcome, 'incomplete');
  assert.equal(record.result?.kind, 'observed');
  assert.equal(record.result?.comparisonState, 'verified');
  assert.equal(record.result?.datasets.length, 7);
  assert.equal(record.result?.missingAuthorities.includes('stable_cut'), true);
});

test('native result parser refuses cross-context, unsafe counts and observed admission claims', () => {
  const mutations = [
    (r: any) => { r.pipeline_id = 'other'; },
    (r: any) => { r.outcome = 'verified'; },
    (r: any) => { r.result_manifest.native_cdc_qualified = true; },
    (r: any) => { r.result_manifest.context_sha256 = 'b'.repeat(64); },
    (r: any) => { delete r.result_manifest.comparison.datasets.raw; },
    (r: any) => { r.result_manifest.comparison.datasets.raw.row_count = Number.MAX_SAFE_INTEGER + 1; },
    (r: any) => { r.result_manifest.format_version = 'future'; },
  ];
  for (const change of mutations) {
    const value = nativeRecordFixture(); change(value);
    assert.throws(() => parseNativeVerification(value, 'pl_fixture'));
  }
  assert.equal(parseNativeVerification(null, 'pl_fixture'), null);
});

test('native API reads and writes use the existing idempotent v2 transport and exact dry-run payload', async () => {
  const seen: [string, RequestInit | undefined][] = [];
  const client = new ControlPlaneV2Client({ fetchFn: async (path, init) => {
    seen.push([path, init]);
    const body = path.endsWith('/capabilities') ? { available: true, mode: 'observation', reason_code: null }
      : init?.method === 'POST' ? { before: null, after: nativeRecordFixture(), dry_run: { would_request: 'native_verification' } }
      : nativeRecordFixture();
    return new Response(JSON.stringify(body), { status: 200 });
  } });
  assert.equal((await client.getNativeCapabilities('pl_fixture')).mode, 'observation');
  assert.equal((await client.getLatestNativeVerification('pl_fixture'))?.id, 'nv_fixture');
  await client.requestNativeVerification('pl_fixture', true);
  await client.requestNativeVerification('pl_fixture', false);
  await client.getNativeVerification('pl_fixture', 'nv_fixture');
  for (const [, init] of seen.filter(([, init]) => init?.method === 'POST')) {
    assert.ok((init?.headers as Record<string, string>)['Idempotency-Key']);
    assert.deepEqual(Object.keys(JSON.parse(init!.body as string)), ['dry_run']);
  }
  assert.deepEqual(seen.filter(([, init]) => init?.method === 'POST').map(([, init]) => JSON.parse(init!.body as string).dry_run), [true, false]);
});

test('context tracking and lifecycle contradictions cannot be presented as a current result', () => {
  for (const change of [
    (r: any) => { r.context_validity = 'tracking_unavailable'; },
    (r: any) => { r.lifecycle = 'running'; },
  ]) {
    const value = nativeRecordFixture(); change(value);
    assert.throws(() => parseNativeVerification(value, 'pl_fixture'));
  }
});

test('admitted results retain their separate format and policy without becoming observations', () => {
  const value: any = nativeRecordFixture();
  value.outcome = 'verified';
  value.result_manifest.format_version = 'quadringent-native-admitted-result-v1';
  value.result_manifest.policy_revision = 'synthetic-policy';
  assert.equal(parseNativeVerification(value, 'pl_fixture')?.result?.kind, 'admitted');
});
