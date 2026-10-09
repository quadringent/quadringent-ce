import assert from 'node:assert/strict';
import test from 'node:test';
import {
  evaluateTableReadiness,
  parseDiscoveryLibraries,
  tableReadinessCopy,
  requiresKeyChoice,
  requiresRrnAcknowledgement,
  isTableStartable,
  type DiscoveredTable,
} from './wizardTables.ts';

function table(overrides: Partial<DiscoveredTable> = {}): DiscoveredTable {
  return {
    id: 't1',
    library: 'DEMOLIB',
    name: "CUSTOMERS",
    approxRowCount: 1000,
    approxSizeBytes: 2_000_000,
    readiness: 'ready',
    keyStrategy: 'unique_index',
    keyColumns: ['ID'],
    clFixCommands: [],
    ...overrides,
  };
}

// `readiness` vient déjà classé du serveur (`services/tables.py::TableRecord`) —
// ce module ne fait que traduire le mot, jamais le recalculer.
test('evaluateTableReadiness is a direct passthrough of the server-classified readiness', () => {
  assert.equal(evaluateTableReadiness(table({ readiness: 'ready' })), 'ready');
  assert.equal(evaluateTableReadiness(table({ readiness: 'not_journaled' })), 'not_journaled');
  assert.equal(evaluateTableReadiness(table({ readiness: 'images_incomplete' })), 'images_incomplete');
  assert.equal(evaluateTableReadiness(table({ readiness: 'no_key' })), 'no_key');
  assert.equal(evaluateTableReadiness(table({ readiness: 'journal_mismatch' })), 'journal_mismatch');
});

test('readiness copy never exposes a raw status code and uses the approved wording', () => {
  assert.equal(tableReadinessCopy('ready').label, "Ready");
  assert.equal(tableReadinessCopy('not_journaled').label, "Not journaled");
  assert.equal(tableReadinessCopy('images_incomplete').label, "Incomplete images");
  assert.equal(tableReadinessCopy('no_key').label, "No key");
  assert.equal(tableReadinessCopy('journal_mismatch').label, "Inconsistent journal");
  for (const state of ['ready', 'not_journaled', 'images_incomplete', 'no_key', 'journal_mismatch'] as const) {
    assert.doesNotMatch(tableReadinessCopy(state).label, /_/);
  }
});

test('a table without a key requires an explicit key choice before it can start', () => {
  assert.equal(requiresKeyChoice(table({ readiness: 'no_key' })), true);
  assert.equal(requiresKeyChoice(table({ readiness: 'ready' })), false);
});

test('choosing RRN as the key requires an explicit acknowledgement of the consequence', () => {
  assert.equal(requiresRrnAcknowledgement('rrn'), true);
  assert.equal(requiresRrnAcknowledgement('unique_index'), false);
});

test('a ready table is startable with no key choice needed', () => {
  assert.equal(isTableStartable(table({ readiness: 'ready' })), true);
});

test('a table blocked on the journal is never startable, whatever the key choice', () => {
  assert.equal(isTableStartable(table({ readiness: 'not_journaled' }), { key: 'rrn', columns: [], rrnAcknowledged: true }), false);
  assert.equal(isTableStartable(table({ readiness: 'images_incomplete' })), false);
  assert.equal(isTableStartable(table({ readiness: 'journal_mismatch' })), false);
});

test('a no_key table needs a chosen unique_index key with at least one column', () => {
  const t = table({ readiness: 'no_key', keyColumns: [] });
  assert.equal(isTableStartable(t), false);
  assert.equal(isTableStartable(t, { key: 'unique_index', columns: [], rrnAcknowledged: false }), false);
  assert.equal(isTableStartable(t, { key: 'unique_index', columns: ['ID'], rrnAcknowledged: false }), true);
});

test('a no_key table can use rrn only once acknowledged', () => {
  const t = table({ readiness: 'no_key', keyColumns: [] });
  assert.equal(isTableStartable(t, { key: 'rrn', columns: [], rrnAcknowledged: false }), false);
  assert.equal(isTableStartable(t, { key: 'rrn', columns: [], rrnAcknowledged: true }), true);
});

test('discovery libraries are explicit and reject empty or wildcard scopes', () => {
  assert.deepEqual(parseDiscoveryLibraries('saleslib, OTHERLIB, saleslib'), ['SALESLIB', 'OTHERLIB']);
  for (const invalid of ['', ' ', 'LIB,', ',LIB', '*ALL', 'LIB/*', 'TOO_LONG_LIB', 'A B']) {
    assert.equal(parseDiscoveryLibraries(invalid), null, invalid);
  }
});
