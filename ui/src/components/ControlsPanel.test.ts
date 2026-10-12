import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let ControlsPanel: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  ControlsPanel = (await vite.ssrLoadModule('/src/components/ControlsPanel.tsx')).ControlsPanel;
});

after(async () => { await vite.close(); });

const noopPromise = () => Promise.resolve(undefined as unknown);

test('renders exactly the actions the level declares available, in French, with no confirmation dialog open', () => {
  const markup = renderToStaticMarkup(createElement(ControlsPanel, {
    level: 'table',
    targetId: 'pl_x',
    targetLabel: "DEMOLIB.ORDERS",
    availableActions: ['pause', 'resume', 'remove'],
    dryRun: noopPromise,
    execute: () => Promise.resolve({ pendingConfirmationId: null }),
    verify: noopPromise,
  }));
  assert.match(markup, />Suspend</);
  assert.match(markup, />Resume</);
  assert.match(markup, />Remove</);
  assert.doesNotMatch(markup, /controls-panel__confirm/);
  assert.doesNotMatch(markup, /Logs/);
});

test('a fleet-level panel only offers the actions it is given — never remove/replay unless declared', () => {
  const markup = renderToStaticMarkup(createElement(ControlsPanel, {
    level: 'fleet',
    targetId: 'fleet',
    targetLabel: 'Toutes les tables',
    availableActions: ['pause', 'resume'],
    dryRun: noopPromise,
    execute: () => Promise.resolve({ pendingConfirmationId: null }),
    verify: noopPromise,
  }));
  assert.doesNotMatch(markup, />Remove</);
  assert.doesNotMatch(markup, />Replay a range</);
});

test('an empty availableActions list renders no action button, never a phantom default', () => {
  const markup = renderToStaticMarkup(createElement(ControlsPanel, {
    level: 'destination',
    targetId: 'dst_x',
    targetLabel: 'DEST_1',
    availableActions: [],
    dryRun: noopPromise,
    execute: () => Promise.resolve({ pendingConfirmationId: null }),
    verify: noopPromise,
  }));
  assert.doesNotMatch(markup, /<button/);
});

test('the panel is keyed by level and target id, so two targets with the same name never share a dialog', () => {
  const props = (targetId: string, targetLabel = 'Laboratory IBM i', level = 'connection') => ({
    level, targetId, targetLabel, availableActions: ['pause'], dryRun: noopPromise,
    execute: () => Promise.resolve({ pendingConfirmationId: null }), verify: noopPromise,
  });
  const keyOf = (value: Record<string, unknown>) => (ControlsPanel(value) as { key: string | null }).key;
  assert.equal(keyOf(props('src_a')), 'connection|src_a');
  assert.notEqual(keyOf(props('src_a')), keyOf(props('src_b')), 'same display name, different source');
  assert.notEqual(keyOf(props('src_a')), keyOf(props('src_a', 'Laboratory IBM i', 'destination')));
  assert.equal(keyOf(props('src_a', 'Renamed')), keyOf(props('src_a', 'Laboratory IBM i')), 'a rename alone does not discard an open dialog');
});

test('no outcome message is shown before any action is requested', () => {
  const markup = renderToStaticMarkup(createElement(ControlsPanel, {
    level: 'table', targetId: 'pl_a', targetLabel: 'pl_a', availableActions: ['pause'], dryRun: noopPromise,
    execute: () => Promise.resolve({ pendingConfirmationId: null }), verify: noopPromise,
  }));
  assert.doesNotMatch(markup, /Declared state confirmed|Effect not confirmed|Result unknown|Request accepted/);
});

test('a panel without any available action says so instead of showing an empty bar', () => {
  const markup = renderToStaticMarkup(createElement(ControlsPanel, {
    level: 'table', targetId: 'pl_a', targetLabel: 'pl_a', availableActions: [], dryRun: noopPromise,
    execute: () => Promise.resolve({ pendingConfirmationId: null }), verify: noopPromise,
  }));
  assert.match(markup, /No action is available in the current state\./);
  assert.doesNotMatch(markup, /<button/);
});
