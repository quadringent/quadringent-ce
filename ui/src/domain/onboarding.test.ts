import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildOnboardingPayload,
  onboardingDraftFor,
  nextOnboardingStep,
  onboardingDefaultsRows,
  onboardingIndex,
  onboardingUnavailableCopy,
  onboardingStepCopy,
  parseOnboardingDefaults,
  parseOnboardingVerdict,
  previousOnboardingStep,
} from './onboarding.ts';
import { formatDuration, sequences } from './format.ts';
import { installTestSite, TEST_SITE, TEST_SITE_WIRE } from './siteFixture.ts';

installTestSite();
const draft = onboardingDraftFor(TEST_SITE);

test('source copy stays simple for a non-technical operator', () => {
  const copy = onboardingStepCopy('source');
  assert.match(`${copy.kicker} ${copy.title}`, /AS400/);
  assert.doesNotMatch(copy.lead, /TLS|CA|Kubernetes|evidence|certif/i);
  assert.equal(copy.primary, "Continue");
});

test('payload never includes a password or token field', () => {
  const payload = buildOnboardingPayload(draft, 'permissions', TEST_SITE);
  const keys = Object.keys(payload).join(' ');
  assert.equal(payload.tls, true);
  assert.equal(payload.allow_plaintext, false);
  assert.equal(payload.tls_ca_file, TEST_SITE.tlsCaFile);
  assert.doesNotMatch(keys, /password|token|secret[^_]|private/i);
  assert.equal(payload.secret_ref_name, TEST_SITE.secretRefName);
});

test('step navigation stays on the unique product path', () => {
  assert.equal(nextOnboardingStep('source'), 'permissions');
  assert.equal(nextOnboardingStep('activate'), null);
  assert.equal(previousOnboardingStep('source'), null);
  assert.equal(previousOnboardingStep('journal'), 'permissions');
});

test('onboarding index exposes three plain-language groups without skipping ahead', () => {
  const items = onboardingIndex('journal');
  assert.equal(items.length, 3);
  assert.deepEqual(items.map((item) => item.label), [
    "Your AS400",
    'Snowflake',
    "Your data",
  ]);
  assert.equal(items[0]?.state, 'current');
  assert.equal(items[0]?.id, 'as400');
  assert.equal(items[0]?.entryStep, 'source');
  assert.equal(items[1]?.state, 'todo');
  assert.equal(items[2]?.index, 3);
});

test('parseOnboardingVerdict keeps proven, unproven and next action explicit', () => {
  const verdict = parseOnboardingVerdict({
    step: 'verdict',
    status: 'review',
    errors: [],
    blocked: [],
    unproven: ["IBM i connectivity not observed"],
    proven: [],
    declared: ['Journal LEDGER.SALE sélectionné'],
    verification_scope: 'configuration_only',
    next_action: "Relire le verdict : ce qui n’est pas observed n’est pas prouvé",
    risk: 'medium',
  });
  assert.equal(verdict.status, 'review');
  assert.deepEqual(verdict.proven, []);
  assert.equal(verdict.declared[0], 'Journal LEDGER.SALE sélectionné');
  assert.match(verdict.nextAction, /n’est pas observed/);
});

test('configuration form never offers self-certified connectivity or pilot success', () => {
  const payload = buildOnboardingPayload(draft, 'pilot', TEST_SITE);
  assert.equal('connectivity' in payload, false);
  assert.equal('pilot' in payload, false);
  assert.match(onboardingStepCopy('pilot').title, /Review/);
  assert.match(onboardingStepCopy('activate').title, /unavailable/);
});

test('legacy or forged runtime verdicts are not accepted as configuration validation', () => {
  for (const extra of [{status:'ready'}, {proven:['Connectivité IBM i observée']}, {verification_scope:undefined}]) {
    assert.throws(() => parseOnboardingVerdict({step:'activate', status:'blocked', risk:'medium',
      next_action:"None Job lancé", proven:[], declared:[], verification_scope:'configuration_only', ...extra}));
  }
});

test('api unavailable copy is never a healthy state', () => {
  const offline = onboardingUnavailableCopy('offline');
  assert.equal(offline.title, "Verification unavailable");
  assert.match(offline.detail, /configuration service/);
});

test('the configuration payload declares the selected tables, never a single one', () => {
  const payload = buildOnboardingPayload(draft, 'journal', TEST_SITE);

  assert.deepEqual(payload.tables, [...TEST_SITE.manifest]);
  assert.equal('table' in payload, false);
});

test('the default selection covers the prepared perimeter', () => {
  assert.deepEqual(draft.tables, [...TEST_SITE.manifest]);
  assert.equal(draft.tables.length, 13);
});

test('a partial selection stays representable for the verdict', () => {
  const payload = buildOnboardingPayload(
    { ...draft, tables: ['SALE', 'CNTR'] },
    'journal',
    TEST_SITE,
  );

  assert.deepEqual(payload.tables, ['SALE', 'CNTR']);
});

test('published onboarding defaults keep only primitives and reject malformed payloads', () => {
  const parsed = parseOnboardingDefaults({
    defaults: {
      batch_entries: 60_000,
      tls: true,
      tls_ca_file: TEST_SITE.tlsCaFile,
      nested: { unexpected: true },
      blank: '   ',
    },
    site: TEST_SITE_WIRE,
  });
  assert.deepEqual(parsed.values, {
    batch_entries: 60_000,
    tls: true,
    tls_ca_file: TEST_SITE.tlsCaFile,
  });
  for (const bad of [null, [], {}, 'x', { defaults: null }, { defaults: [] }, { defaults: 'x' }]) {
    assert.throws(() => parseOnboardingDefaults(bad), /Invalid published settings/);
  }
  assert.throws(() => parseOnboardingDefaults({ defaults: {} }), /site/);
});

test('onboarding default rows carry French business labels and never round the published values', () => {
  const rows = onboardingDefaultsRows(parseOnboardingDefaults({
    defaults: {
      batch_entries: 60_000,
      poll_seconds: 2,
      pilot_max_seconds: 600,
      replica_count_at_rest: 0,
      tls: true,
      allow_plaintext: false,
      tls_ca_file: TEST_SITE.tlsCaFile,
      undocumented_key: 7,
    },
    site: TEST_SITE_WIRE,
  }));
  const byKey = new Map(rows.map((row) => [row.key, row]));

  assert.equal(byKey.get('batch_entries')?.label, "Reader batch size");
  assert.equal(byKey.get('batch_entries')?.value, sequences(60_000));
  assert.equal(byKey.get('poll_seconds')?.value, formatDuration(2));
  assert.equal(byKey.get('pilot_max_seconds')?.value, formatDuration(600));
  assert.equal(byKey.get('replica_count_at_rest')?.value, "None");
  assert.equal(byKey.get('tls')?.value, "Required");
  assert.equal(byKey.get('allow_plaintext')?.value, "Disallowed");
  assert.equal(byKey.get('tls_ca_file')?.value, TEST_SITE.tlsCaFile);
  assert.equal(byKey.get('undocumented_key')?.label, 'undocumented_key');
  assert.equal(byKey.get('undocumented_key')?.value, '7');
});
