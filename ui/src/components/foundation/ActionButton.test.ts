import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let ActionButton: ComponentType<any>;
let actionConfirmCopy: (label: string) => string;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  ActionButton = (await vite.ssrLoadModule('/src/components/foundation/ActionButton.tsx')).ActionButton;
  actionConfirmCopy = (await vite.ssrLoadModule('/src/domain/operator.ts')).actionConfirmCopy;
});

after(async () => { await vite.close(); });

test('un bouton sans confirmation requise ne rend aucune boîte de dialogue', () => {
  const markup = renderToStaticMarkup(createElement(ActionButton, { label: "Refresh", onAction() {} }));
  assert.match(markup, />Refresh</);
  assert.doesNotMatch(markup, /<dialog/);
});

test('une action destructrice rend une boîte de dialogue de confirmation avec le texte d’operator.ts', () => {
  const markup = renderToStaticMarkup(createElement(ActionButton, {
    label: "Pause", onAction() {}, requiresConfirmation: true,
  }));
  assert.match(markup, /<dialog/);
  // La boîte n'est pas ouverte tant qu'aucune interaction n'a eu lieu : son
  // contenu (le texte de confirmation) n'est pas rendu.
  assert.doesNotMatch(markup, /<p id="action-button-confirm-title"/);
  assert.equal(actionConfirmCopy("Pause"), "Confirm “Pause”? This action will execute immediately.");
});

test('disabled reflète le contrat produit : bouton inactif quand la capacité n’est pas disponible', () => {
  const markup = renderToStaticMarkup(createElement(ActionButton, { label: "Restart", onAction() {}, disabled: true }));
  assert.match(markup, /disabled=""/);
});

test('primary applique le modificateur visuel dédié', () => {
  const markup = renderToStaticMarkup(createElement(ActionButton, { label: "Confirm", onAction() {}, primary: true }));
  assert.match(markup, /action-button--primary/);
});
