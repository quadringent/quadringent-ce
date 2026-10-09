import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import { workspaceEvidenceMode } from '../domain/pipelineView.ts';
import { resolveProofFocus } from '../domain/proofFocus.ts';
import { scopeLabel, sourceAvailability } from '../domain/scope.ts';
import { connectionCopy, evidenceStateLabel } from './AppShell.model.ts';

const snapshotFormatter = new Intl.DateTimeFormat("en-US", { hourCycle: 'h23',
  day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
});

export interface EvidencePresentation {
  readonly label: string;
  readonly shortLabel: string;
  readonly tone: 'simulation' | 'live' | 'unknown';
}

export function evidencePresentation(state: ControlPlaneState): EvidencePresentation {
  if (state.status === 'loading' || state.status === 'failed') {
    return { label: "Data state unconfirmed", shortLabel: "NOT ESTABLISHED", tone: 'unknown' };
  }

  const workspaceMode = workspaceEvidenceMode(state.overview);
  const proofFocus = resolveProofFocus(state);
  const proofScope = proofFocus.proofScope;
  const availability = sourceAvailability(state.overview);

  if (proofScope.kind === 'cached') {
    return { label: "Last retained observation · offline", shortLabel: 'CACHE', tone: 'unknown' };
  }
  if (availability === 'unestablished' || availability === 'unavailable' || workspaceMode === 'unestablished' || proofScope.kind === 'unavailable') {
    return { label: "Data state unconfirmed", shortLabel: "NOT ESTABLISHED", tone: 'unknown' };
  }
  if (availability === 'partial' || proofScope.kind === 'partial') {
    return { label: "Partial information", shortLabel: "PARTIAL", tone: 'unknown' };
  }
  if (workspaceMode === 'mixed' || proofScope.kind === 'mixed') {
    return { label: "Data with different provenances", shortLabel: "MIXED", tone: 'unknown' };
  }
  if (proofScope.kind === 'stale') {
    return {
      label: workspaceMode === 'simulation'
        ? "Demonstration · not up to date"
        : workspaceMode === 'historical'
          ? "Historical observation · not up to date"
          : "Observation not up to date",
      shortLabel: workspaceMode === 'simulation' ? 'SIM' : workspaceMode === 'historical' ? 'HIST' : "NOT CURRENT",
      tone: workspaceMode === 'live' ? 'unknown' : 'simulation',
    };
  }
  if (workspaceMode === 'simulation') return { label: "Demonstration", shortLabel: 'SIM', tone: 'simulation' };
  if (workspaceMode === 'historical') return { label: "Historical observation", shortLabel: 'HIST', tone: 'simulation' };
  if (proofScope.kind === 'live' && workspaceMode === 'live') {
    return proofFocus.firstBreak
      ? { label: "Data to verify", shortLabel: "NOT ESTABLISHED", tone: 'unknown' }
      : { label: "Recent observation", shortLabel: 'LIVE', tone: 'live' };
  }
  return { label: "Data state unconfirmed", shortLabel: "NOT ESTABLISHED", tone: 'unknown' };
}

export function EvidenceContext({ state }: { readonly state: ControlPlaneState }) {
  const hasSnapshot = state.status !== 'loading' && state.status !== 'failed';
  const evidence = evidencePresentation(state);
  const currentTransport = evidence.tone === 'live' && evidence.shortLabel === 'LIVE';
  const connection = connectionCopy(state.connection, state.status, currentTransport ? 'current' : 'technical');
  const scope = scopeLabel(hasSnapshot ? state.overview.scope : null);
  const snapshot = hasSnapshot ? state.overview.generatedAt : null;
  const receivedAt = hasSnapshot ? state.lastSuccessAt : null;

  return (
    <section className="evidence-context" aria-label="Data details">
      <header>
        <p>Data details</p>
        <strong>{scope}</strong>
      </header>
      <dl>
        <div><dt>Data state</dt><dd>{evidenceStateLabel(evidence.shortLabel)}</dd></div>
        <div><dt>Service</dt><dd>{connection.label}</dd></div>
        <div>
          <dt>Observation from</dt>
          <dd>{snapshot ? <time dateTime={snapshot}>{snapshotFormatter.format(new Date(snapshot))} UTC</time> : "Unavailable"}</dd>
        </div>
        <div>
          <dt>Received on</dt>
          <dd>{receivedAt ? <time dateTime={receivedAt.toISOString()}>{snapshotFormatter.format(receivedAt)} UTC</time> : "Not received"}</dd>
        </div>
      </dl>
    </section>
  );
}
