import { SYNTHETIC_DEMO_COPY } from '../domain/operator.ts';

export const DEMO_NETWORK_ERROR = SYNTHETIC_DEMO_COPY.networkRefused;

interface NetworkScope {
  fetch: typeof fetch;
  EventSource?: unknown;
  WebSocket?: unknown;
  XMLHttpRequest?: unknown;
  navigator?: { sendBeacon?: (url: string, data?: unknown) => boolean };
}

function refusingConstructor(): new (...args: unknown[]) => never {
  return class {
    constructor() { throw new Error(DEMO_NETWORK_ERROR); }
  } as unknown as new (...args: unknown[]) => never;
}

/**
 * Synthetic demo only: every network API is refused in code, in addition to
 * the page's `connect-src 'none'` policy. Demo clients use in-memory fetch
 * functions and never call these globals.
 */
export function installDemoNetworkGuard(scope: NetworkScope): void {
  scope.fetch = (() => Promise.reject(new Error(DEMO_NETWORK_ERROR))) as typeof fetch;
  scope.EventSource = refusingConstructor();
  scope.WebSocket = refusingConstructor();
  scope.XMLHttpRequest = class {
    open(): never { throw new Error(DEMO_NETWORK_ERROR); }
    send(): never { throw new Error(DEMO_NETWORK_ERROR); }
  };
  if (scope.navigator) scope.navigator.sendBeacon = () => false;
}
