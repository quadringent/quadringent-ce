import assert from 'node:assert/strict';
import { test } from 'node:test';
import { age, decimal, exact, formatDuration, position, sequences, timestamp } from './format.ts';

const THIN = ' ';

test('sequence numbers retain every digit and their sign', () => {
  assert.equal(sequences(30320398), '30,320,398');
  assert.equal(sequences(1), '1');
  assert.equal(sequences(142580), "142,580");
  assert.equal(sequences(-142580), "-142,580");
  assert.equal(position('RCV0001', 30320398), 'RCV0001 · 30,320,398');
});

test('measured decimals retain requested precision', () => {
  assert.equal(decimal(0.5463, 4), '0.5463');
  assert.equal(decimal(30, 0), '30');
});

test('measurements do not receive trailing decimal padding', () => {
  assert.equal(exact(0.5463), '0.5463');
  assert.equal(exact(30.69), '30.69');
  assert.equal(exact(30), '30');
});

test('duration units remain consistent across seconds, minutes, hours and days', () => {
  assert.equal(formatDuration(8), '8' + THIN + 's');
  assert.equal(formatDuration(174), '2' + THIN + 'min' + THIN + '54' + THIN + 's');
  assert.equal(formatDuration(196), '3' + THIN + 'min' + THIN + '16' + THIN + 's');
  assert.equal(formatDuration(600), '10' + THIN + 'min');
  assert.equal(formatDuration(1247), '20' + THIN + 'min' + THIN + '47' + THIN + 's');
  assert.equal(formatDuration(3600), '1' + THIN + 'h');
  assert.equal(formatDuration(18540), '5' + THIN + 'h' + THIN + '09');
  assert.equal(formatDuration(90501), '25' + THIN + 'h' + THIN + '08');
  assert.equal(formatDuration(140000), '38' + THIN + 'h' + THIN + '53');
  assert.equal(formatDuration(200000), '2' + THIN + 'd');
});

test('freshness thresholds are preserved with English relative dates', () => {
  const now = new Date('2026-08-28T10:00:00Z');
  assert.equal(age('2026-08-28T09:59:30Z', now).stale, false);
  assert.equal(age('2026-08-27T22:00:00Z', now).stale, true);
  assert.equal(age('2026-08-28T09:00:00Z', now).label, '1' + THIN + 'h ago');
  assert.equal(age('2026-08-28T09:59:58Z', now).label, 'just now');
  assert.equal(age('2026-08-28T09:58:00Z', now).stale, false);
});

test('evidence timestamps use UTC irrespective of the input offset', () => {
  assert.equal(timestamp('2026-08-28T10:33:01+02:00'), '2026-08-28 08:33:01 UTC');
  assert.equal(timestamp('2026-08-28T08:33:01.123Z'), '2026-08-28 08:33:01 UTC');
});
