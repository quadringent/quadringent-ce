/**
 * Orchestration of the controls panel: effect announced (dry run) ->
 * confirmation -> execution -> read-back. It is independent of React so the
 * guarantees can be tested without a DOM:
 *
 *  - the state is mirrored synchronously, so a double click is one request;
 *  - the success message only exists after a read-back confirmed the effect;
 *  - a request without a usable answer is an unknown result, and the only
 *    way forward is reading the state again, never a second mutation;
 *  - results that arrive after the panel left the screen are dropped.
 */
import {
  ActionTimeoutError,
  controlsPanelReducer,
  describeActionError,
  describeVerifyError,
  INITIAL_CONTROLS_PANEL_STATE,
  type ControlActionKind,
  type ControlsPanelEvent,
  type ControlsPanelState,
} from '../domain/controlsPanel.ts';

export const DEFAULT_ACTION_TIMEOUT_MS = 30_000;

export interface VerifyContext {
  readonly action: ControlActionKind;
  /** What the service answered when it accepted the request (`null` after an unknown result). */
  readonly accepted: unknown;
  /** The effect announced before the confirmation. */
  readonly preview: unknown;
  readonly signal: AbortSignal;
}

export interface FlowDeps {
  readonly timeoutMs: number;
  readonly dryRun: (action: ControlActionKind, signal: AbortSignal) => Promise<unknown>;
  readonly execute: (
    action: ControlActionKind,
    confirmationToken: string | undefined,
    signal: AbortSignal,
  ) => Promise<{ readonly pendingConfirmationId: string | null; readonly accepted?: unknown }>;
  /** Reads the state again; throws when the effect is not confirmed, and may
   *  return a short description of what it confirmed (for example the state). */
  readonly verify: (context: VerifyContext) => Promise<string | void>;
  readonly approveConfirmation?: (confirmationId: string) => Promise<void>;
  readonly rejectConfirmation?: (confirmationId: string) => Promise<void>;
}

export interface ControlsFlow {
  readonly state: () => ControlsPanelState;
  readonly request: (action: ControlActionKind) => void;
  readonly confirm: (confirmationToken?: string) => void;
  readonly cancel: () => void;
  readonly approve: () => void;
  readonly reject: () => void;
  readonly checkState: () => void;
  readonly attach: () => void;
  readonly detach: () => void;
}

/** Runs `start` with an abort signal that fires when `timeoutMs` elapses. */
export function runWithTimeout<T>(start: (signal: AbortSignal) => Promise<T>, timeoutMs: number): Promise<T> {
  const controller = new AbortController();
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      controller.abort();
      reject(new ActionTimeoutError());
    }, timeoutMs);
    const settle = <V>(finish: (value: V) => void) => (value: V) => {
      clearTimeout(timer);
      finish(value);
    };
    try {
      start(controller.signal).then(settle(resolve), settle(reject));
    } catch (error) {
      settle(reject)(error);
    }
  });
}

export function createControlsFlow(getDeps: () => FlowDeps, onState: (state: ControlsPanelState) => void): ControlsFlow {
  let state = INITIAL_CONTROLS_PANEL_STATE;
  let attached = false;
  let accepted: unknown = null;
  /** Synchronous guard for the approval round trip, which has no phase of its own. */
  let deciding = false;
  /** Bumped whenever a new cycle starts; late results of an older one are dropped. */
  let cycle = 0;

  const send = (event: ControlsPanelEvent): ControlsPanelState => {
    state = controlsPanelReducer(state, event);
    if (attached) onState(state);
    return state;
  };

  const live = (id: number) => attached && id === cycle;

  const runVerify = (id: number, action: ControlActionKind, acceptedValue: unknown, preview: unknown) => {
    const deps = getDeps();
    runWithTimeout((signal) => deps.verify({ action, accepted: acceptedValue, preview, signal }), deps.timeoutMs)
      .then(
        (confirmed) => { if (live(id)) send({ type: 'verified', confirmed: typeof confirmed === 'string' ? confirmed : null }); },
        (error: unknown) => { if (live(id)) send({ type: 'verify_failed', error: describeVerifyError(error) }); },
      );
  };

  const request = (action: ControlActionKind) => {
    const before = state;
    const next = send({ type: 'request', action });
    // The reducer ignores a request mid-cycle and returns the same state.
    if (next === before || next.phase !== 'previewing') return;
    cycle += 1;
    accepted = null;
    const id = cycle;
    const deps = getDeps();
    runWithTimeout((signal) => deps.dryRun(action, signal), deps.timeoutMs).then(
      (preview) => { if (live(id)) send({ type: 'preview_ready', preview }); },
      (error: unknown) => {
        if (live(id)) send({ type: 'preview_failed', error: error instanceof Error && error.message ? error.message : 'Preview unavailable.' });
      },
    );
  };

  const confirm = (confirmationToken?: string) => {
    const before = state;
    const next = send({ type: 'confirm' });
    if (next.phase !== 'running' || before.phase === 'running') return;
    const action = next.action;
    if (!action) return;
    const id = cycle;
    const preview = next.preview;
    const deps = getDeps();
    runWithTimeout((signal) => deps.execute(action, confirmationToken, signal), deps.timeoutMs).then(
      (result) => {
        if (!live(id)) return;
        if (result.pendingConfirmationId) {
          send({ type: 'run_requires_confirmation', confirmationId: result.pendingConfirmationId });
          return;
        }
        accepted = result.accepted ?? null;
        send({ type: 'run_succeeded' });
        runVerify(id, action, accepted, preview);
      },
      (error: unknown) => {
        if (!live(id)) return;
        const described = describeActionError(error);
        send({ type: 'run_failed', error: described.message, failure: described.failure });
      },
    );
  };

  const decide = (kind: 'approve' | 'reject') => {
    if (deciding || state.phase !== 'awaiting_confirmation' || state.confirmationState !== 'pending' || !state.confirmationId) return;
    const deps = getDeps();
    const call = kind === 'approve' ? deps.approveConfirmation : deps.rejectConfirmation;
    if (!call) return;
    deciding = true;
    const id = cycle;
    const confirmationId = state.confirmationId;
    runWithTimeout(() => call(confirmationId), deps.timeoutMs).then(
      () => {
        deciding = false;
        if (live(id)) send({ type: kind === 'approve' ? 'confirmation_approved' : 'confirmation_rejected' });
      },
      (error: unknown) => {
        deciding = false;
        if (live(id)) send({ type: 'approval_failed', error: describeActionError(error).message });
      },
    );
  };

  return {
    state: () => state,
    request,
    confirm,
    cancel: () => { send({ type: 'cancel' }); },
    approve: () => decide('approve'),
    reject: () => decide('reject'),
    checkState: () => {
      const next = send({ type: 'check_state' });
      if (next.phase !== 'verifying' || !next.action) return;
      runVerify(cycle, next.action, null, next.preview);
    },
    attach: () => { attached = true; },
    detach: () => { attached = false; },
  };
}
