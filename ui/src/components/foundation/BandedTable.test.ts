import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

interface Row { readonly id: string; readonly name: string; readonly rows: number; }

let vite: ViteDevServer;
let BandedTable: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  BandedTable = (await vite.ssrLoadModule('/src/components/foundation/BandedTable.tsx')).BandedTable;
});

after(async () => { await vite.close(); });

const rows: readonly Row[] = [
  { id: 'a', name: "CUSTOMER", rows: 1204 },
  { id: 'b', name: "ORDER", rows: 88031 },
];

const columns = [
  { key: 'name', header: 'Table', render: (row: Row) => row.name },
  { key: 'rows', header: "Rows", numeric: true, render: (row: Row) => row.rows.toLocaleString("en-US") },
];

test('les colonnes numériques rendent leurs cellules en mono, alignées à droite', () => {
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
  }));
  assert.match(markup, /banded-table__col--numeric mono/);
  assert.match(markup, /1,204/);
});

test('chaque ligne est focalisable au clavier, une seule à la fois dans l’ordre de tabulation', () => {
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
  }));
  const tabIndexes = [...markup.matchAll(/tabindex="(-?\d)"/g)].map((match) => match[1]);
  assert.deepEqual(tabIndexes, ['0', '-1']);
});

test('un emplacement d’actions par ligne se rend en dernière colonne quand il est fourni', () => {
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
    rowActions: (row: Row) => createElement('button', { type: 'button' }, `Inspect ${row.name}`),
  }));
  assert.match(markup, /Inspect CUSTOMER/);
  assert.match(markup, /banded-table__col--actions/);
});

test('sans emplacement d’actions, aucune colonne d’actions n’est rendue', () => {
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
  }));
  assert.doesNotMatch(markup, /banded-table__col--actions/);
});

test('l’en-tête d’une colonne numérique est aligné à droite comme ses valeurs', () => {
  // La règle générique `.banded-table thead th` (0,1,2) l’emporterait sur une
  // simple classe `.banded-table__col--numeric` (0,1,0) : l’en-tête resterait
  // à gauche au-dessus de chiffres alignés à droite.
  const css = readFileSync(new URL('../../styles/foundation.css', import.meta.url), 'utf8');
  const rule = /\.banded-table thead th\.banded-table__col--numeric\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule, 'règle d’en-tête numérique absente');
  assert.match(rule[1], /text-align:\s*right/);
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
  }));
  assert.match(markup, /<th scope="col" class="banded-table__col--numeric">Rows<\/th>/);
});
