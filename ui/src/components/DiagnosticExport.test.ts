import assert from 'node:assert/strict';
import test, { before, after } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let View: any;
before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  View = (await vite.ssrLoadModule('/src/components/DiagnosticExport.tsx')).DiagnosticExportView;
});
after(async () => { await vite.close(); });

test('download is absent until the service declares it available', () => {
  assert.equal(renderToStaticMarkup(createElement(View, { available: false, phase: 'idle', onDownload() {} })), '');
  const markup = renderToStaticMarkup(createElement(View, { available: true, phase: 'idle', onDownload() {} }));
  assert.match(markup, /Download diagnostic/);
  assert.match(markup, /No live checks/);
});

test('pending download is disabled and failure or download start remains explicit', () => {
  const pending = renderToStaticMarkup(createElement(View, { available: true, phase: 'downloading', onDownload() {} }));
  assert.match(pending, /disabled/);
  assert.match(pending, /Preparing diagnostic/);
  const failed = renderToStaticMarkup(createElement(View, { available: true, phase: 'failed', onDownload() {} }));
  assert.match(failed, /Download failed/);
  assert.doesNotMatch(failed, /Download started/);
  const started = renderToStaticMarkup(createElement(View, { available: true, phase: 'started', onDownload() {} }));
  assert.match(started, /Download started/);
  assert.match(started, /Review before sharing/);
  assert.doesNotMatch(started, /Saved locally/);
});
