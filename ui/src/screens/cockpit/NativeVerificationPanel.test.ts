import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import { nativeRecordFixture } from '../../data/nativeVerification.testFixtures.ts';
import { parseNativeVerification } from '../../data/nativeVerification.ts';

let vite: ViteDevServer;
let Panel: ComponentType<any>;
before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  Panel = (await vite.ssrLoadModule('/src/screens/cockpit/NativeVerificationPanel.tsx')).NativeVerificationPanel;
});
after(async () => { await vite.close(); });
function render(overrides = {}) {
  return renderToStaticMarkup(createElement(Panel, { data: { pipeline: { id: 'pl_fixture' }, ...overrides }, onSettled() {} }));
}

test('missing proof or capability cannot offer execution', () => {
  const html = render();
  assert.match(html, /No verification recorded/);
  assert.doesNotMatch(html, /<button[^>]*>Run native verification/);
});

test('observed equality displays seven layers, missing authorities and no green qualification', () => {
  const html = render({ nativeVerification: parseNativeVerification(nativeRecordFixture(), 'pl_fixture'),
    nativeCapabilities: { available: true, mode: 'observation', reasonCode: null } });
  for (const label of ['Source', 'Snapshot', 'Journal', 'Positions', 'RAW', 'HISTORY', 'MIRROR', 'Incomplete', 'Stable cut', 'Destination identity']) assert.ok(html.includes(label), label);
  assert.match(html, /Run native verification/);
  assert.match(html, /Equality does not establish native qualification/);
  assert.doesNotMatch(html, /evidence-state--observed|status-word--ok/);
});

test('superseded, unavailable tracking, divergence and read errors stay explicit', () => {
  const record = parseNativeVerification(nativeRecordFixture(), 'pl_fixture')!;
  for (const contextValidity of ['superseded', 'tracking_unavailable']) {
    assert.match(render({ nativeVerification: { ...record, contextValidity } }), contextValidity === 'superseded' ? /Superseded context/ : /Context tracking unavailable/);
  }
  assert.match(render({ nativeVerification: { ...record, result: { ...record.result, comparisonState: 'divergent' } } }), /Differences found/);
  assert.match(render({ nativeVerificationError: true }), /Verification could not be read/);
});
