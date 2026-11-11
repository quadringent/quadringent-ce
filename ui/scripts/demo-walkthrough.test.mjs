import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { test } from 'node:test';
import { createCockpitDemoFetch } from '../src/data/fixtures/cockpitDemo.ts';

const guideUrl = new URL('../../docs/product/synthetic-demo.md', import.meta.url);
const guide = readFileSync(guideUrl, 'utf8').replace(/\s+/g, ' ');

async function evidence(id) {
  const response = await createCockpitDemoFetch()(`/v2/pipelines/${id}/evidence`);
  assert.equal(response.status, 200);
  return response.json();
}

test('the walkthrough distinguishes synthetic capture, delivery and missing reconciliation', async () => {
  const result = await evidence('pl_sales_customers');
  assert.ok(guide.includes('## A first evidence check'));
  assert.ok(guide.includes('Sales → CUSTOMERS → Evidence'));
  assert.ok(guide.includes(`${result.reading.event_count} captured events`));
  assert.ok(guide.includes(`${result.delivery.event_count} delivered events`));
  assert.equal(result.evidence_kind, 'simulation');
  assert.equal(result.reconciliation.state, 'absent');
  assert.ok(guide.includes('reconciliation is Not measured'));
  assert.ok(guide.includes('These are generated values'));
});

test('the walkthrough uses the real stale-delivery scenario', async () => {
  const result = await evidence('pl_sales_order_lines');
  assert.equal(result.delivery.state, 'stale');
  assert.ok(guide.includes('Sales → ORDER_LINES → Evidence'));
  assert.ok(guide.includes('Synthetic, stale'));
  assert.ok(guide.includes(`${result.delivery.event_count} events`));
});

test('the walkthrough does not turn absent evidence into a zero', async () => {
  const result = await evidence('pl_sales_archive');
  assert.equal(result.evidence_kind, 'absent');
  for (const stage of ['reading', 'delivery', 'reconciliation']) {
    assert.equal(result[stage].state, 'absent');
    assert.equal(result[stage].event_count, null);
  }
  assert.ok(guide.includes('Sales → ARCHIVE_90D → Evidence'));
  assert.ok(guide.includes('No stage has a measurement'));
  assert.ok(guide.includes('missing measurement is not a count of zero'));
});

test('the demo has a next step to real installation without requesting credentials', () => {
  assert.ok(guide.includes('## Move to your first real table'));
  for (const target of ['compatibility.md', 'install-default.md', '../evidence.md']) {
    assert.ok(guide.includes(`](${target})`));
    assert.ok(existsSync(new URL(target, guideUrl)));
  }
  assert.ok(guide.includes('The demo does not copy your data or test your credentials'));
  assert.ok(guide.includes('non-production IBM i'));
});
