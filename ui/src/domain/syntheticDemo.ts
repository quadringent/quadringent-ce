import type { Route } from '../router.ts';

/** Routes that the synthetic demo can render without any service. */
export type DemoRoute = Extract<Route, {
  name: 'cockpit' | 'cockpit-connection' | 'cockpit-table' | 'cockpit-confirmations' | 'wizard';
}>;

export const DEMO_HOME: DemoRoute = { name: 'cockpit' };

/** Build provenance injected by `ui/scripts/build-demo.mjs`. */
export interface SyntheticDemoProvenance {
  readonly version: string;
  readonly sourceCommit: string;
  readonly builtAtUtc: string;
}

/** v1 screens and sign-in need a running service: the demo falls back to
 *  its cockpit home and says so rather than inventing those states. */
export function demoRoute(route: Route): { readonly route: DemoRoute; readonly unavailable: boolean } {
  switch (route.name) {
    case 'cockpit':
    case 'cockpit-connection':
    case 'cockpit-table':
    case 'cockpit-confirmations':
    case 'wizard':
      return { route, unavailable: false };
    default:
      return { route: DEMO_HOME, unavailable: true };
  }
}
