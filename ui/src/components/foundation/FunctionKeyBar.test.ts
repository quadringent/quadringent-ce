import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let FunctionKeyBar: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  FunctionKeyBar = (await vite.ssrLoadModule('/src/components/foundation/FunctionKeyBar.tsx')).FunctionKeyBar;
});

after(async () => { await vite.close(); });

const entries = [
  { key: 'F3', label: "Go back", disabled: false },
  { key: 'F5', label: "Refresh", disabled: false },
  { key: 'F9', label: "Pause/Resume", disabled: true },
  { key: 'F12', label: "Logs", disabled: false },
];

test('chaque touche annonce son raccourci clavier réel via aria-keyshortcuts', () => {
  const markup = renderToStaticMarkup(createElement(FunctionKeyBar, { entries, onTrigger() {} }));
  assert.match(markup, /aria-keyshortcuts="F3"/);
  assert.match(markup, /aria-keyshortcuts="F5"/);
  assert.match(markup, /aria-keyshortcuts="F9"/);
  assert.match(markup, /aria-keyshortcuts="F12"/);
  assert.match(markup, />Go back</);
  assert.match(markup, />Refresh</);
  assert.match(markup, />Pause\/Resume</);
  assert.match(markup, />Logs</);
});

test('une entrée désactivée rend le bouton disabled', () => {
  const markup = renderToStaticMarkup(createElement(FunctionKeyBar, { entries, onTrigger() {} }));
  const pauseButton = markup.split("Pause/Resume")[0]!.split('<button').pop()!;
  assert.match(pauseButton, /disabled=""/);
});

test('sans entrée, la barre ne rend rien', () => {
  const markup = renderToStaticMarkup(createElement(FunctionKeyBar, { entries: [], onTrigger() {} }));
  assert.equal(markup, '');
});
