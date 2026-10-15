import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { ConfirmationRecord } from '../../data/controlPlaneV2Client.ts';

let vite: ViteDevServer;
let View: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  View = (await vite.ssrLoadModule('/src/screens/cockpit/CockpitConfirmations.tsx')).CockpitConfirmationsView;
});
after(async () => { await vite.close(); });

const pending: ConfirmationRecord = {
  id: 'cf_1', actionRef: 'pipeline.restart_initial_copy', resourceType: 'pipeline', resourceId: 'pl_1',
  reason: "Initial copy demandée", riskEstimate: null, requestedByKind: 'agent', requestedById: 'agent_1',
  expiresAt: '2026-09-29T09:00:00Z', state: 'pending', approvedByKind: null, approvedById: null,
  approvedAt: null, createdAt: '2026-09-28T09:00:00Z', available: { approve: true, reject: true, execute: false },
};

function render(state: unknown): string {
  return renderToStaticMarkup(createElement(View, { state, onRefresh() {}, onDecide() {} }));
}

test('loading, failure and empty inbox each have distinct content', () => {
  assert.match(render({ status: 'loading' }), /Reading/);
  assert.match(render({ status: 'failed', message: "Unavailable" }), /Unavailable/);
  assert.match(render({ status: 'ready', items: [] }), /No pending decisions/);
});

test('pending item shows scope, reason and only server-declared decisions', () => {
  const markup = render({ status: 'ready', items: [pending] });
  assert.match(markup, /Restart the initial copy/);
  assert.match(markup, /Initial copy demandée/);
  assert.match(markup, /#\/cockpit\/table\/pl_1/);
  assert.match(markup, />Approve</);
  assert.match(markup, />Reject</);
  const denied = render({ status: 'ready', items: [{ ...pending, available: { approve: false, reject: false, execute: false } }] });
  assert.doesNotMatch(denied, />Approve</);
  assert.doesNotMatch(denied, />Reject</);
});

test('an approved copy can be explicitly executed, then verified, without approving again', () => {
  const approved = render({ status: 'ready', items: [{ ...pending, state: 'approved',
    available: { approve: false, reject: false, execute: true } }] });
  assert.match(approved, /Approved/);
  assert.match(approved, />Execute action</);
  assert.doesNotMatch(approved, />Approve</);
});

test('an unreadable expiry date is worded in English, never in another language', () => {
  const markup = render({ status: 'ready', items: [{ ...pending, expiresAt: 'not-a-date' }] });
  assert.match(markup, /Expires on unknown date/);
  assert.doesNotMatch(markup, /date inconnue/);
});

test('requester wording and labels come from the operator copy', () => {
  const markup = render({ status: 'ready', items: [pending, { ...pending, id: 'cf_2', requestedByKind: 'human', requestedById: null }] });
  assert.match(markup, /Requested by the agent agent_1/);
  assert.match(markup, /Requested by an operator</);
  assert.match(markup, /Pending request/);
  assert.match(markup, /aria-label="Confirmations to review"/);
  assert.match(markup, /Table pl_1/);
});

test('the screen holds no hard-coded user-facing English or French sentences outside operator.ts', async () => {
  const { readFileSync } = await import('node:fs');
  const source = readFileSync(new URL('./CockpitConfirmations.tsx', import.meta.url), 'utf8');
  for (const literal of ['date inconnue', 'Operator decision', 'Requested by', 'Expires on', 'Reported risk', 'Pending request',
    'Verification in progress', 'Quadringent is querying', 'Service unavailable', 'did not confirm', 'cannot be executed from this screen',
    'Confirmations to review', '/ Decisions', '>Refresh<', '>Reading<']) {
    assert.ok(!source.includes(literal), `${literal} must come from CONFIRMATIONS_COPY`);
  }
});
