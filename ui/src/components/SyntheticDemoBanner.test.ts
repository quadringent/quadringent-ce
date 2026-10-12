import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let SyntheticDemoBanner: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  SyntheticDemoBanner = (await vite.ssrLoadModule('/src/components/SyntheticDemoBanner.tsx')).SyntheticDemoBanner;
});

after(async () => { await vite.close(); });

const provenance = {
  version: '0.3.5',
  sourceCommit: '1baec99b5afe44396630faf153a38425ffbe13db',
  builtAtUtc: '2026-10-12T18:00:00Z',
};

test('the banner states synthetic data, the absence of connection and the build provenance', () => {
  const html = renderToStaticMarkup(createElement(SyntheticDemoBanner, { provenance }));
  assert.match(html, /role="region"/);
  assert.match(html, /aria-label="Synthetic demo"/);
  assert.match(html, /Synthetic demo/);
  assert.match(html, /No IBM i, Snowflake or cloud connection/);
  assert.match(html, /0\.3\.5/);
  assert.match(html, /1baec99/);
  assert.match(html, /2026-10-12T18:00:00Z/);
  assert.match(html, /not a release qualification/);
});

test('the banner never presents demo values as measurements', () => {
  const html = renderToStaticMarkup(createElement(SyntheticDemoBanner, { provenance }));
  assert.match(html, /Every value is generated locally/);
  assert.match(html, /including values labelled measured or observed/);
});
