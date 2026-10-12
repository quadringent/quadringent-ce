import assert from 'node:assert/strict';
import test from 'node:test';
import { DEMO_NETWORK_ERROR, installDemoNetworkGuard } from './demoNetworkGuard.ts';

function target() {
  return {
    fetch: async () => new Response('real'),
    EventSource: class {},
    WebSocket: class {},
    XMLHttpRequest: class { open() {} },
    navigator: { sendBeacon: () => true },
  } as unknown as Parameters<typeof installDemoNetworkGuard>[0];
}

test('fetch is refused instead of reaching any service', async () => {
  const scope = target();
  installDemoNetworkGuard(scope);
  await assert.rejects(() => scope.fetch('/v2/pipelines'), { message: DEMO_NETWORK_ERROR });
  await assert.rejects(() => scope.fetch('https://example.invalid/'), { message: DEMO_NETWORK_ERROR });
});

test('streaming, sockets, XHR and beacons are refused', () => {
  const scope = target() as any;
  installDemoNetworkGuard(scope);
  assert.throws(() => new scope.EventSource('/v1/events'), { message: DEMO_NETWORK_ERROR });
  assert.throws(() => new scope.WebSocket('ws://127.0.0.1/'), { message: DEMO_NETWORK_ERROR });
  assert.throws(() => new scope.XMLHttpRequest().open('GET', '/v2/sources'), { message: DEMO_NETWORK_ERROR });
  assert.equal(scope.navigator.sendBeacon('/v2/telemetry', ''), false);
});

test('the refusal message explains the synthetic demo', () => {
  assert.match(DEMO_NETWORK_ERROR, /Synthetic demo/);
  assert.match(DEMO_NETWORK_ERROR, /no network/);
});
