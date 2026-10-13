import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlPlaneV2Client, ControlPlaneV2Error, parseRetryAfter } from './controlPlaneV2Client.ts';

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}

function clientFor(handler: (input: string, init?: RequestInit) => Response | Promise<Response>) {
  return new ControlPlaneV2Client({ fetchFn: async (input, init) => handler(input, init) });
}

function result(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    code: 'delivery.snowflake',
    layer: 'delivery',
    status: 'failed',
    required: true,
    observed_at: '2026-10-12T18:59:30Z',
    provenance: 'control-plane:evidence',
    subject: { kind: 'pipeline', ref: 'pl_123', label: 'SALES.ORDERS' },
    since: '2026-10-12T18:40:00Z',
    summary: 'Delivery to Snowflake is stale.',
    next_action: 'Open the table logs.',
    limits: 'Collected by the loader log; not a reconciliation.',
    evidence: { stage_state: 'stale', evidence_kind: 'live' },
    ...overrides,
  };
}

function report(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema: 'quadringent.diagnostic.v1',
    generated_at: '2026-10-12T19:00:00Z',
    scope: 'control_plane',
    synthetic: false,
    software: { product: 'quadringent', version: '0.3.5', distribution: 'control-plane' },
    overall: { status: 'blocked', passed: 0, failed: 1, unknown: 0, skipped: 0, summary: 'One required check failed.' },
    results: [result()],
    ...overrides,
  };
}

const SHA = 'a'.repeat(64);

function exportPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema: 'quadringent.diagnostic-export.v1',
    generated_at: '2026-10-12T19:00:00Z',
    preview: {
      files: [{ name: 'report.json', bytes: 12, sha256: SHA }],
      omitted: [{ field: 'source.ibmi_host', reason: 'pseudonymized by default' }],
      pseudonymized: { hosts: 1, accounts: 1, identifiers: 4 },
      limits: ['Not a reconciliation.'],
    },
    files: { 'report.json': '{"a":"b"}' },
    ...overrides,
  };
}

test('getDiagnostics parses a complete report and sends the optional pipeline filter', async () => {
  const urls: string[] = [];
  const client = clientFor((input, init) => {
    urls.push(input);
    assert.equal(init?.method, 'GET');
    assert.equal((init?.headers as Record<string, string> | undefined)?.['Idempotency-Key'], undefined);
    return jsonResponse(200, report());
  });
  const all = await client.getDiagnostics();
  await client.getDiagnostics({ pipelineId: 'pl 1/x' });
  assert.deepEqual(urls, ['/v2/diagnostics/report', '/v2/diagnostics/report?pipeline_id=pl%201%2Fx']);
  assert.equal(all.scope, 'control_plane');
  assert.equal(all.synthetic, false);
  assert.equal(all.overall.status, 'blocked');
  const first = all.results[0]!;
  assert.equal(first.layer, 'delivery');
  assert.equal(first.status, 'failed');
  assert.equal(first.required, true);
  assert.deepEqual(first.subject, { kind: 'pipeline', ref: 'pl_123', label: 'SALES.ORDERS' });
  assert.equal(first.since, '2026-10-12T18:40:00Z');
  assert.equal(first.evidenceKind, 'live');
  assert.equal(first.evidence.stage_state, 'stale');
});

test('subject, since and next_action may be null and an absent evidence kind stays absent', async () => {
  const client = clientFor(() => jsonResponse(200, report({
    results: [result({ subject: null, since: null, next_action: null, limits: null, evidence: { stage_state: 'stale' } })],
  })));
  const [first] = (await client.getDiagnostics()).results;
  assert.equal(first!.subject, null);
  assert.equal(first!.since, null);
  assert.equal(first!.nextAction, null);
  assert.equal(first!.evidenceKind, null);
});

test('a report with an unknown status, layer, schema or evidence kind is rejected as a whole', async () => {
  const defects: Record<string, Record<string, unknown>> = {
    status: report({ results: [result(), result({ status: 'healthy' })] }),
    layer: report({ results: [result({ layer: 'storage' })] }),
    schema: report({ schema: 'quadringent.diagnostic.v2' }),
    evidence_kind: report({ results: [result({ evidence: { evidence_kind: 'estimated' } })] }),
    overall: report({ overall: { status: 'healthy', passed: 1, failed: 0, unknown: 0, skipped: 0, summary: '' } }),
    scope: report({ scope: 'cloud' }),
    synthetic: (() => { const value = report(); delete value.synthetic; return value; })(),
    nested_evidence: report({ results: [result({ evidence: { nested: { a: 1 } } })] }),
    timestamp: report({ results: [result({ observed_at: 'yesterday' })] }),
    subject_kind: report({ results: [result({ subject: { kind: 'planet', ref: 'x', label: null } })] }),
    count: report({ overall: { status: 'blocked', passed: -1, failed: 1, unknown: 0, skipped: 0, summary: '' } }),
  };
  for (const [name, payload] of Object.entries(defects)) {
    const client = clientFor(() => jsonResponse(200, payload));
    await assert.rejects(
      () => client.getDiagnostics(),
      (error: unknown) => {
        assert.ok(error instanceof ControlPlaneV2Error, name);
        assert.equal((error as ControlPlaneV2Error).kind, 'invalid_response', name);
        return true;
      },
      name,
    );
  }
});

test('getDiagnosticsExport parses the preview and rejects a malformed digest', async () => {
  const ok = await clientFor(() => jsonResponse(200, exportPayload())).getDiagnosticsExport();
  assert.deepEqual(ok.preview.files, [{ name: 'report.json', bytes: 12, sha256: SHA }]);
  assert.deepEqual(ok.preview.omitted, [{ field: 'source.ibmi_host', reason: 'pseudonymized by default' }]);
  assert.deepEqual(ok.preview.pseudonymized, { hosts: 1, accounts: 1, identifiers: 4 });
  assert.deepEqual(ok.preview.limits, ['Not a reconciliation.']);
  assert.equal(ok.files['report.json'], '{"a":"b"}');

  const bad = exportPayload({ preview: { ...(exportPayload().preview as object), files: [{ name: 'report.json', bytes: 12, sha256: 'xyz' }] } });
  await assert.rejects(() => clientFor(() => jsonResponse(200, bad)).getDiagnosticsExport(), /Invalid diagnostic payload/);
  const wrongSchema = exportPayload({ schema: 'quadringent.diagnostic.v1' });
  await assert.rejects(() => clientFor(() => jsonResponse(200, wrongSchema)).getDiagnosticsExport(), /unsupported schema/);
  const traversal = exportPayload({ preview: { ...(exportPayload().preview as object), files: [{ name: '../etc/passwd', bytes: 1, sha256: SHA }] } });
  await assert.rejects(() => clientFor(() => jsonResponse(200, traversal)).getDiagnosticsExport(), /file name/);
});

test('HTTP failures are distinguished: session, forbidden, not found, rate limited, unavailable', async () => {
  const cases: readonly [number, string, string][] = [
    [401, 'insufficient_role', 'session'],
    [403, 'insufficient_role', 'forbidden'],
    [404, 'not_found', 'not_found'],
    [429, 'internal_error', 'rate_limited'],
    [500, 'internal_error', 'unavailable'],
    [503, 'store_unavailable', 'unavailable'],
  ];
  for (const [status, code, kind] of cases) {
    const client = clientFor(() => jsonResponse(status, { error: { code, message: 'x', next_action: 'y', retryable: false } }));
    await assert.rejects(
      () => client.getDiagnostics(),
      (error: unknown) => {
        assert.ok(error instanceof ControlPlaneV2Error);
        assert.equal(error.status, status);
        assert.equal(error.kind, kind, `status ${status}`);
        return true;
      },
    );
  }
});

test('a 429 keeps Retry-After in seconds, from a number or an HTTP date, and ignores garbage', async () => {
  const withHeader = (value: string) => clientFor(() => jsonResponse(429, { error: { code: 'internal_error', message: 'slow down', next_action: 'wait', retryable: true } }, { 'Retry-After': value }));
  const read = async (value: string) => {
    try { await withHeader(value).getDiagnostics(); } catch (error) { return (error as ControlPlaneV2Error).retryAfterSeconds; }
    return undefined;
  };
  assert.equal(await read('30'), 30);
  assert.equal(await read('not-a-delay'), null);
  assert.equal(parseRetryAfter('Wed, 21 Oct 2026 07:28:30 GMT', new Date('2026-10-21T07:28:00Z')), 30);
  assert.equal(parseRetryAfter('Wed, 21 Oct 2026 07:28:30 GMT', new Date('2026-10-21T08:00:00Z')), 0);
  assert.equal(parseRetryAfter(null), null);
});

test('a non-JSON gateway error and a network failure both read as service unavailable', async () => {
  const gateway = clientFor(() => new Response('<html>bad gateway</html>', { status: 502 }));
  await assert.rejects(() => gateway.getDiagnostics(), (error: unknown) => (error as ControlPlaneV2Error).kind === 'unavailable');
  const offline = new ControlPlaneV2Client({ fetchFn: async () => { throw new TypeError('Failed to fetch'); } });
  await assert.rejects(() => offline.getDiagnosticsExport(), (error: unknown) => {
    assert.ok(error instanceof ControlPlaneV2Error);
    assert.equal(error.kind, 'unavailable');
    assert.equal(error.status, 0);
    return true;
  });
});

test('a cancelled request keeps its AbortError instead of reading as an outage', async () => {
  const controller = new AbortController();
  const client = new ControlPlaneV2Client({
    fetchFn: async (_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    }),
  });
  const pending = client.getDiagnostics({}, controller.signal);
  controller.abort();
  await assert.rejects(pending, (error: unknown) => error instanceof DOMException && error.name === 'AbortError');
});

test('a network failure during a mutation is classified as unavailable, never as success', async () => {
  const client = new ControlPlaneV2Client({ fetchFn: async () => { throw new TypeError('network down'); } });
  await assert.rejects(
    () => client.runPipelineAction('pl_1', 'pause'),
    (error: unknown) => (error as ControlPlaneV2Error).kind === 'unavailable',
  );
});

test('the shape the control plane really emits is accepted: null label, null evidence values, partial software', async () => {
  const client = clientFor(() => jsonResponse(200, report({
    software: { product: 'quadringent' },
    results: [result({
      subject: { kind: 'table', ref: 'tbl_1', label: null },
      evidence: { stage_state: 'absent', event_count: null, evidence_kind: 'absent' },
    })],
  })));
  const [first] = (await client.getDiagnostics()).results;
  assert.equal(first!.subject!.label, null);
  assert.equal(first!.evidence.event_count, null);
  assert.equal(first!.evidenceKind, 'absent');
});
