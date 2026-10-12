/**
 * Wiring between the controls panel and the v2 client for each cockpit level.
 * Execution returns what the service answered; verification reads the state
 * again and throws unless the effect is visible, so the panel can say
 * "effect not confirmed" instead of assuming success.
 */
import {
  ControlPlaneV2Error,
  pendingConfirmationId,
  type ControlPlaneV2Client,
} from '../../data/controlPlaneV2Client.ts';
import type { ControlsPanelProps } from '../../components/ControlsPanel.tsx';
import {
  EffectNotConfirmedError,
  expectedDeclaredState,
  requiresConfirmation,
  scopeExpectations,
  type ControlActionKind,
} from '../../domain/controlsPanel.ts';
import { CONTROLS_PANEL_COPY } from '../../domain/operator.ts';

export interface ActionWiring {
  readonly getClient: () => Promise<ControlPlaneV2Client>;
  /** Called after every read-back, confirmed or not, so the screen shows the real state. */
  readonly onSettled: () => void;
}

type PanelWiring = Pick<ControlsPanelProps, 'dryRun' | 'execute' | 'verify' | 'approveConfirmation' | 'rejectConfirmation'>;

function pendingFrom(error: unknown): string | null {
  return error instanceof ControlPlaneV2Error ? pendingConfirmationId(error) : null;
}

function decisions({ getClient }: ActionWiring): Pick<PanelWiring, 'approveConfirmation' | 'rejectConfirmation'> {
  return {
    approveConfirmation: async (confirmationId) => { await (await getClient()).approveConfirmation(confirmationId); },
    rejectConfirmation: async (confirmationId) => { await (await getClient()).rejectConfirmation(confirmationId); },
  };
}

export function tableControls(pipelineId: string, wiring: ActionWiring): PanelWiring {
  return {
    dryRun: async (action, signal) => (await wiring.getClient()).runPipelineAction(pipelineId, action, { dryRun: true }, signal),
    execute: async (action, confirmationToken, signal) => {
      const client = await wiring.getClient();
      try {
        const envelope = await client.runPipelineAction(pipelineId, action, { confirmationToken }, signal);
        return { pendingConfirmationId: null, accepted: envelope.after };
      } catch (error) {
        const confirmationId = pendingFrom(error);
        if (confirmationId) return { pendingConfirmationId: confirmationId };
        throw error;
      }
    },
    verify: async ({ action, accepted, preview, signal }) => {
      const client = await wiring.getClient();
      try {
        let current;
        try {
          current = await client.getPipeline(pipelineId, signal);
        } catch (error) {
          // A removed table is expected to disappear from the service.
          if (action === 'remove' && error instanceof ControlPlaneV2Error && error.status === 404) return;
          throw error;
        }
        if (action === 'replay') throw new EffectNotConfirmedError(CONTROLS_PANEL_COPY.notConfirmedReplay);
        const expected = expectedDeclaredState(accepted, preview);
        if (!expected) throw new EffectNotConfirmedError(CONTROLS_PANEL_COPY.notConfirmedNoReference);
        if (current.declaredState !== expected) {
          throw new EffectNotConfirmedError(CONTROLS_PANEL_COPY.notConfirmedMismatch(current.declaredState, expected));
        }
      } finally {
        wiring.onSettled();
      }
    },
    ...decisions(wiring),
  };
}

export type ScopeTarget =
  | { readonly level: 'connection' | 'destination'; readonly id: string }
  | { readonly level: 'fleet' };

export function scopeControls(target: ScopeTarget, wiring: ActionWiring): PanelWiring {
  const run = (client: ControlPlaneV2Client, action: ControlActionKind, options: { readonly dryRun?: boolean; readonly confirmationToken?: string }, signal?: AbortSignal) => {
    if (requiresConfirmation(action) || (action !== 'pause' && action !== 'resume')) {
      throw new Error(`Action not supported at ${target.level} level.`);
    }
    if (target.level === 'connection') return client.runSourceAction(target.id, action, options, signal);
    if (target.level === 'destination') return client.runDestinationAction(target.id, action, options, signal);
    return client.runFleetAction(action === 'pause' ? 'pause_all' : 'resume_all', options, signal);
  };
  return {
    dryRun: async (action, signal) => run(await wiring.getClient(), action, { dryRun: true }, signal),
    execute: async (action, confirmationToken, signal) => {
      const client = await wiring.getClient();
      try {
        const envelope = await run(client, action, { confirmationToken }, signal);
        return { pendingConfirmationId: null, accepted: envelope.after };
      } catch (error) {
        const confirmationId = pendingFrom(error);
        if (confirmationId) return { pendingConfirmationId: confirmationId };
        throw error;
      }
    },
    verify: async ({ accepted, signal }) => {
      const client = await wiring.getClient();
      try {
        const expectations = scopeExpectations(accepted);
        if (!expectations) throw new EffectNotConfirmedError(CONTROLS_PANEL_COPY.notConfirmedNoReference);
        const reads = await Promise.all(expectations.map(async (expectation) => ({
          expectation,
          current: await client.getPipeline(expectation.pipelineId, signal),
        })));
        const mismatched = reads.filter(({ expectation, current }) => current.declaredState !== expectation.declaredState).length;
        if (mismatched > 0) {
          throw new EffectNotConfirmedError(CONTROLS_PANEL_COPY.notConfirmedScopeMismatch(mismatched, reads.length));
        }
      } finally {
        wiring.onSettled();
      }
    },
    ...decisions(wiring),
  };
}
