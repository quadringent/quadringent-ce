import assert from 'node:assert/strict';
import test from 'node:test';
import { demoRoute, DEMO_HOME } from './syntheticDemo.ts';

test('cockpit and wizard routes stay available in the synthetic demo', () => {
  for (const route of [
    { name: 'cockpit' },
    { name: 'cockpit-confirmations' },
    { name: 'cockpit-diagnostics' },
    { name: 'cockpit-diagnostics', pipelineId: 'pl_sales_orders' },
    { name: 'cockpit-connection', id: 'src_demo_sales' },
    { name: 'cockpit-table', id: 'pl_sales_order_lines', tab: 'proofs' },
    { name: 'wizard', step: 'source' },
  ] as const) {
    assert.deepEqual(demoRoute(route), { route, unavailable: false });
  }
});

test('v1, login and other service routes fall back to the cockpit home and say so', () => {
  for (const route of [{ name: 'overview' }, { name: 'setup' }, { name: 'login' }, { name: 'usage' }] as const) {
    assert.deepEqual(demoRoute(route as never), { route: DEMO_HOME, unavailable: true });
  }
});
