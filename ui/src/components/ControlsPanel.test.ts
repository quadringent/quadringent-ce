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
    targetLabel: 'DEST_1',
    availableActions: [],
    dryRun: noopPromise,
    execute: () => Promise.resolve({ pendingConfirmationId: null }),
    verify: noopPromise,
  }));
  assert.doesNotMatch(markup, /<button/);
});
