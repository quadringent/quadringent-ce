import type { ConnectionState } from '../data/controlPlaneController.ts';
import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import { resolveProofFocus } from '../domain/proofFocus.ts';
import { scopeLabel } from '../domain/scope.ts';

export interface ConnectionCopy {
  readonly label: string;
  readonly detail: string;
}

export type WorkspaceFocusScheduler = (focusWorkspace: () => void) => void;

interface MobileDetailsTarget {
  open: boolean;
  querySelector(selector: string): { focus(): void } | null;
}

interface MobileDetailsKeyEvent {
  readonly key: string;
  readonly currentTarget: MobileDetailsTarget;
  preventDefault(): void;
  stopPropagation(): void;
}

export function connectionCopy(
  connection: ConnectionState,
  status?: ControlPlaneState['status'],
  transport: 'current' | 'technical' = 'current',
): ConnectionCopy {
  if (status === 'refreshing' || status === 'loading') {
    return { label: "Verification in progress", detail: "The service is being checked" };
  }
  if (status === 'degraded' || status === 'failed') {
    return { label: "Unavailable", detail: "The service is unavailable" };
  }
  switch (connection) {
    case 'live':
      return { label: "Available", detail: transport === 'technical'
        ? "The service responds; data state is shown separately"
        : "The service responds" };
    case 'reconnecting':
      return { label: "Reconnecting", detail: "The service connection is recovering" };
    case 'offline':
      return { label: "Offline", detail: "The service connection is interrupted" };
    case 'connecting':
    default:
      return { label: "Connecting", detail: "The service connection is being established" };
  }
}

export function closeMobileDetailsOnEscape(event: MobileDetailsKeyEvent): boolean {
  if (event.key !== 'Escape' || !event.currentTarget.open) return false;
  event.preventDefault();
  event.stopPropagation();
  event.currentTarget.open = false;
  event.currentTarget.querySelector('summary')?.focus();
  return true;
}

export function liveAnnouncement(state: ControlPlaneState): string {
  const service = connectionCopy(state.connection, state.status).detail;
  const scope = scopeLabel(state.status === 'loading' || state.status === 'failed' ? null : state.overview.scope);
  if (state.status === 'loading' || state.status === 'failed') {
    return `${service}. Environment: ${scope}. Data unavailable.`;
  }
  const focus = resolveProofFocus(state);
  const dataState = announcementDataState(focus);
  return `${service}. Environment: ${scope}. ${dataState}.`;
}

function announcementDataState(focus: ReturnType<typeof resolveProofFocus>): string {
  switch (focus.proofScope.kind) {
    case 'live':
      return focus.focus.kind === 'completion' ? "Data available in Snowflake" : "Data to verify";
    case 'simulation':
      return "Demonstration data";
    case 'historical':
      return "Last historical observation";
    case 'cached':
      return "Last retained observation";
    case 'stale':
      return "Observation not up to date";
    case 'partial':
    case 'mixed':
      return "Partial information";
    case 'unavailable':
      return "Data unavailable";
  }
}

/** Libellé court et lisible de l'état des données affiché dans la coquille. */
export function evidenceStateLabel(shortLabel: string): string {
  switch (shortLabel) {
    case 'LIVE': return "Recent observation";
    case 'SIM': return "Demonstration";
    case 'HIST': return "Historical observation";
    case 'CACHE': return "Retained observation";
    case "PARTIAL": return "Partial information";
    case "NOT CURRENT": return "Not up to date";
    default: return "To verify";
  }
}

export function scheduleWorkspaceFocus(focusWorkspace: () => void): void {
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => focusWorkspace());
    return;
  }
  if (typeof queueMicrotask === 'function') {
    queueMicrotask(focusWorkspace);
    return;
  }
  setTimeout(focusWorkspace, 0);
}

export function skipWorkspaceInteraction(
  event: { preventDefault(): void },
  focusWorkspace: () => void,
  scheduleFocus: WorkspaceFocusScheduler = scheduleWorkspaceFocus,
): void {
  event.preventDefault();
  scheduleFocus(focusWorkspace);
}

export function restoreWorkspaceAfterNavigation(
  scrollToOrigin: () => void,
  focusWorkspace: () => void,
  scheduleFocus: WorkspaceFocusScheduler = scheduleWorkspaceFocus,
): void {
  scrollToOrigin();
  scheduleFocus(focusWorkspace);
}
