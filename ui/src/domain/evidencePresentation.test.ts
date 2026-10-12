import assert from 'node:assert/strict';
import test from 'node:test';
import { evidenceKindView, evidenceStageView } from './evidencePresentation.ts';
import type { EvidenceStage } from '../data/controlPlaneV2Client.ts';

const stage = (state: EvidenceStage['state'], observedAt: string | null = '2026-10-10T12:00:00Z'): EvidenceStage => ({
  state, observedAt: state === 'absent' ? null : observedAt, eventCount: state === 'absent' ? null : 3,
  lagSeconds: null, duplicateCount: null, detail: 'detail',
});

test('only a live observed stage is shown as an observation', () => {
  assert.deepEqual(
    { word: evidenceStageView(stage('observed'), 'live').word, tone: evidenceStageView(stage('observed'), 'live').tone },
    { word: 'Observed', tone: 'observed' },
  );
});

test('synthetic and historical observations keep their limit and never turn green', () => {
  const synthetic = evidenceStageView(stage('observed'), 'simulation');
  assert.equal(synthetic.tone, 'limit');
  assert.equal(synthetic.word, 'Synthetic');
  const historical = evidenceStageView(stage('observed'), 'historical');
  assert.equal(historical.tone, 'limit');
  assert.equal(historical.word, 'Historical');
});

test('a stale stage is a limit with its last observation time', () => {
  const view = evidenceStageView(stage('stale', '2026-10-09T08:00:00Z'), 'live');
  assert.equal(view.word, 'Stale');
  assert.equal(view.tone, 'limit');
  assert.equal(view.observedAt, '2026-10-09T08:00:00Z');
});

test('an absent stage is missing: no timestamp, no count, no green', () => {
  for (const kind of ['live', 'historical', 'simulation', 'absent'] as const) {
    const view = evidenceStageView(stage('absent'), kind);
    assert.equal(view.word, 'Not measured');
    assert.equal(view.tone, 'missing');
    assert.equal(view.observedAt, null);
  }
});

test('evidence kinds reuse the v1 caveats so both cockpits say the same thing', () => {
  assert.equal(evidenceKindView('live').caveat, null);
  assert.match(evidenceKindView('simulation').caveat ?? '', /do not come from your system/);
  assert.match(evidenceKindView('historical').caveat ?? '', /not the current state/);
  assert.equal(evidenceKindView('absent').label, 'No evidence collected');
  assert.equal(evidenceKindView('absent').tone, 'missing');
  assert.equal(evidenceKindView('simulation').tone, 'limit');
  assert.equal(evidenceKindView('live').tone, 'observed');
});

test('a stale synthetic or historical stage keeps its kind in the word', () => {
  assert.equal(evidenceStageView(stage('stale'), 'simulation').word, 'Synthetic, stale');
  assert.equal(evidenceStageView(stage('stale'), 'historical').word, 'Historical, stale');
  assert.equal(evidenceStageView(stage('stale'), 'live').word, 'Stale');
});
