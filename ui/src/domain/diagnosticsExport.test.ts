import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import type { DiagnosticExport } from '../data/diagnosticsContract.ts';
import {
  checkExportIntegrity,
  downloadExport,
  exportBundleText,
  exportFileName,
  exportResultCount,
  type BlobDownloadEnv,
} from './diagnosticsExport.ts';

const sha = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');

function exportFor(files: Record<string, string>, overrides: Partial<DiagnosticExport['preview']> = {}): DiagnosticExport {
  return {
    schema: 'quadringent.diagnostic-export.v1',
    generatedAt: '2026-10-12T19:00:00Z',
    preview: {
      files: Object.entries(files).map(([name, text]) => ({ name, bytes: new TextEncoder().encode(text).length, sha256: sha(text) })),
      omitted: [{ field: 'source.ibmi_host', reason: 'pseudonymized by default' }],
      pseudonymized: { hosts: 1 },
      limits: ['Not a reconciliation.'],
      ...overrides,
    },
    files,
  };
}

test('an export that matches its preview passes the integrity check', async () => {
  const result = await checkExportIntegrity(exportFor({ 'report.json': '{"a":1}', 'README.txt': 'héllo' }));
  assert.deepEqual(result, { problems: [], verified: true });
});

test('a changed file, a missing file and an unlisted file are each reported', async () => {
  const good = exportFor({ 'report.json': '{"a":1}', 'manifest.json': '{}' });
  const tamperedFiles = { ...good.files, 'report.json': '{"a":2}' };
  const tampered = await checkExportIntegrity({ ...good, files: tamperedFiles });
  assert.ok(tampered.problems.some((problem) => /report\.json has a different SHA-256/.test(problem)));

  const resized = await checkExportIntegrity({ ...good, files: { ...good.files, 'report.json': '{"a":12}' } });
  assert.ok(resized.problems.some((problem) => /report\.json has a different size/.test(problem)));

  const { 'manifest.json': _removed, ...withoutManifest } = good.files;
  const missing = await checkExportIntegrity({ ...good, files: withoutManifest });
  assert.ok(missing.problems.some((problem) => /manifest\.json is listed but its content is missing/.test(problem)));

  const unlisted = await checkExportIntegrity({ ...good, files: { ...good.files, 'extra.txt': 'secret' } });
  assert.ok(unlisted.problems.some((problem) => /extra\.txt is present but not listed/.test(problem)));
});

test('without a digest function the export is marked unverified, not valid', async () => {
  const result = await checkExportIntegrity(exportFor({ 'report.json': '{}' }), null);
  assert.equal(result.verified, false);
  assert.deepEqual(result.problems, []);
});

test('the file name keeps only the digits of the generation time', () => {
  assert.equal(exportFileName('2026-10-12T19:00:00Z'), 'quadringent-diagnostic-export-20261012T190000Z.json');
  assert.equal(exportFileName('2026-10-12T19:00:00+02:00'), 'quadringent-diagnostic-export-20261012T170000Z.json');
});

test('the bundle is the received JSON, with its preview and files and nothing added', () => {
  const exported = exportFor({ 'report.json': '{"a":1}' });
  const parsed = JSON.parse(exportBundleText(exported)) as Record<string, unknown>;
  assert.deepEqual(Object.keys(parsed).sort(), ['files', 'generated_at', 'preview', 'schema']);
  assert.equal(parsed.schema, 'quadringent.diagnostic-export.v1');
  assert.deepEqual(parsed.files, { 'report.json': '{"a":1}' });
  const preview = parsed.preview as Record<string, unknown>;
  assert.deepEqual(Object.keys(preview).sort(), ['files', 'limits', 'omitted', 'pseudonymized']);
});

function fakeEnv(log: string[], options: { readonly clickThrows?: boolean } = {}): BlobDownloadEnv & { run: () => void } {
  let scheduled: (() => void) | null = null;
  return {
    createObjectURL: (blob) => { log.push(`create:${blob.type}:${blob.size}`); return 'blob:fake-1'; },
    revokeObjectURL: (url) => { log.push(`revoke:${url}`); },
    click: (url, fileName) => {
      log.push(`click:${url}:${fileName}`);
      if (options.clickThrows) throw new Error('click failed');
    },
    schedule: (callback) => { scheduled = callback; },
    run: () => scheduled?.(),
  };
}

test('download builds an in-memory JSON blob, clicks once and revokes the object URL afterwards', () => {
  const log: string[] = [];
  const env = fakeEnv(log);
  downloadExport(exportFor({ 'report.json': '{}' }), env);
  assert.equal(log.length, 2, 'the URL is not revoked before the browser used it');
  assert.match(log[0]!, /^create:application\/json:\d+$/);
  assert.equal(log[1], 'click:blob:fake-1:quadringent-diagnostic-export-20261012T190000Z.json');
  env.run();
  assert.equal(log[2], 'revoke:blob:fake-1');
  assert.equal(log.length, 3);
});

test('the object URL is revoked even when the click fails', () => {
  const log: string[] = [];
  const env = fakeEnv(log, { clickThrows: true });
  assert.throws(() => downloadExport(exportFor({ 'report.json': '{}' }), env), /click failed/);
  env.run();
  assert.ok(log.includes('revoke:blob:fake-1'));
});

test('no persistent browser storage is touched by the export module', async () => {
  const { readFileSync } = await import('node:fs');
  const source = readFileSync(new URL('./diagnosticsExport.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|document\.cookie/);
});

test('the number of results covered by the export is read from report.json, or unknown', () => {
  const report = JSON.stringify({ results: [{}, {}, {}] });
  assert.equal(exportResultCount(exportFor({ 'report.json': report })), 3);
  assert.equal(exportResultCount(exportFor({ 'report.json': '{"results": []}' })), 0);
  assert.equal(exportResultCount(exportFor({ 'report.json': 'not json' })), null);
  assert.equal(exportResultCount(exportFor({ 'report.json': '{"results": "x"}' })), null);
  assert.equal(exportResultCount(exportFor({ 'README.txt': 'no report' })), null);
});
