/**
 * Machine à états pure du panneau de contrôles réutilisé à chaque niveau du
 * cockpit (accueil/flotte, connexion/source, table — docs/plans/
 * 2026-09-23-produit-fini-design.md §3 « contrôles à chaque niveau, toujours
 * au même endroit »). Aucun accès réseau ici : `ControlsPanel.tsx` appelle
 * cette machine et lui fournit les résultats de `controlPlaneV2Client.ts`.
 *
 * Parcours imposé par le produit : effet annoncé (dry_run) → confirmation →
 * exécution → vérification par relecture (jamais un simple « ça a dû marcher »).
 * Une action sensible peut répondre `pending_confirmation_required` : le
 * panneau reste ouvert sur « en attente d'approbation » jusqu'à ce que la
 * confirmation soit approuvée, puis l'opérateur relance explicitement.
 */

import { CONTROLS_PANEL_COPY } from './operator.ts';

export type ControlActionKind = 'pause' | 'resume' | 'restart_initial_copy' | 'remove' | 'replay';
export type ControlLevel = 'table' | 'connection' | 'destination' | 'fleet';
export type ControlsPanelPhase =
  | 'idle'
  | 'previewing'
  | 'preview_ready'
  | 'running'
  | 'awaiting_confirmation'
  | 'verifying'
  | 'succeeded'
  | 'failed';

/**
 * Why a panel ended in `failed`:
 *  - `refused`: the service answered and refused; nothing was changed;
 *  - `rejected`: the confirmation was rejected; nothing was executed;
 *  - `unknown_result`: no usable answer (timeout, transport or server error)
 *    after the request was sent, so the action may have been applied;
 *  - `not_confirmed`: the request was accepted but reading the state again
 *    did not confirm the effect;
 *  - `error`: any other failure before or without a mutation.
 */
export type ControlFailure = 'refused' | 'rejected' | 'unknown_result' | 'not_confirmed' | 'error';

export interface ControlsPanelState {
  readonly phase: ControlsPanelPhase;
  readonly action: ControlActionKind | null;
  readonly preview: unknown | null;
  readonly confirmationId: string | null;
  readonly confirmationState: 'pending' | 'approved' | 'rejected' | null;
  readonly error: string | null;
  readonly failure: ControlFailure | null;
}

export type ControlsPanelEvent =
  | { readonly type: 'request'; readonly action: ControlActionKind }
  | { readonly type: 'preview_ready'; readonly preview: unknown }
  | { readonly type: 'preview_failed'; readonly error: string }
  | { readonly type: 'confirm' }
  | { readonly type: 'cancel' }
  | { readonly type: 'run_succeeded' }
  | { readonly type: 'run_requires_confirmation'; readonly confirmationId: string }
  | { readonly type: 'run_failed'; readonly error: string; readonly failure?: 'refused' | 'unknown_result' | 'error' }
  | { readonly type: 'approval_failed'; readonly error: string }
  | { readonly type: 'check_state' }
  | { readonly type: 'confirmation_approved' }
  | { readonly type: 'confirmation_rejected' }
  | { readonly type: 'verified' }
  | { readonly type: 'verify_failed'; readonly error: string }
  | { readonly type: 'reset' };

export const INITIAL_CONTROLS_PANEL_STATE: ControlsPanelState = {
  phase: 'idle',
  action: null,
  preview: null,
  confirmationId: null,
  confirmationState: null,
  error: null,
  failure: null,
};

export function controlsPanelReducer(state: ControlsPanelState, event: ControlsPanelEvent): ControlsPanelState {
  switch (event.type) {
    case 'request':
      if (state.phase !== 'idle' && state.phase !== 'succeeded' && state.phase !== 'failed') return state;
      return { ...INITIAL_CONTROLS_PANEL_STATE, phase: 'previewing', action: event.action };

    case 'preview_ready':
      if (state.phase !== 'previewing') return state;
      return { ...state, phase: 'preview_ready', preview: event.preview };

    case 'preview_failed':
      if (state.phase !== 'previewing') return state;
      return { ...state, phase: 'failed', error: event.error };

    case 'confirm':
      if (state.phase === 'preview_ready') return { ...state, phase: 'running' };
      if (state.phase === 'awaiting_confirmation' && state.confirmationState === 'approved') {
        return { ...state, phase: 'running', error: null };
      }
      return state;

    case 'cancel':
      if (state.phase === 'preview_ready' || state.phase === 'failed') return INITIAL_CONTROLS_PANEL_STATE;
      return state;

    case 'run_succeeded':
      if (state.phase !== 'running') return state;
      return { ...state, phase: 'verifying' };

    case 'run_requires_confirmation':
      if (state.phase !== 'running') return state;
      return { ...state, phase: 'awaiting_confirmation', confirmationId: event.confirmationId, confirmationState: 'pending' };

    case 'run_failed':
      if (state.phase !== 'running') return state;
      return { ...state, phase: 'failed', error: event.error, failure: event.failure ?? 'error' };

    case 'approval_failed':
      // The panel stays on the pending confirmation: nothing was approved.
      if (state.phase !== 'awaiting_confirmation') return state;
      return { ...state, error: event.error };

    case 'check_state':
      // Reading again is the only way out of an unknown result: it never
      // sends a second mutation.
      if (state.phase !== 'failed' || state.failure !== 'unknown_result' || !state.action) return state;
      return { ...state, phase: 'verifying', error: null, failure: null };

    case 'confirmation_approved':
      if (state.phase !== 'awaiting_confirmation') return state;
      return { ...state, confirmationState: 'approved', error: null };

    case 'confirmation_rejected':
      if (state.phase !== 'awaiting_confirmation') return state;
      return { ...state, phase: 'failed', confirmationState: 'rejected', error: "Confirmation rejected: action not executed.", failure: 'rejected' };

    case 'verified':
      if (state.phase !== 'verifying') return state;
      return { ...state, phase: 'succeeded' };

    case 'verify_failed':
      if (state.phase !== 'verifying') return state;
      return { ...state, phase: 'failed', error: event.error, failure: 'not_confirmed' };

    case 'reset':
      return INITIAL_CONTROLS_PANEL_STATE;
  }
}

/** Actions considérées sensibles par le contrat (§2.4) : jamais exécutées
 *  sans confirmation approuvée, quel que soit le niveau. */
const SENSITIVE_ACTIONS = new Set<ControlActionKind>(['remove', 'restart_initial_copy', 'replay']);

export function requiresConfirmation(action: ControlActionKind): boolean {
  return SENSITIVE_ACTIONS.has(action);
}

/* ------------------------------------------------------------------------
 * Outcome explanations. An accepted request is not a confirmed effect, and a
 * request without a usable answer is not a failure: the copy says which of
 * the two the operator is looking at.
 * ------------------------------------------------------------------------ */

/** Thrown by a read-back that completed but did not show the expected effect. */
export class EffectNotConfirmedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'EffectNotConfirmedError';
  }
}

/** Raised when the service gave no answer within the allotted time. */
export class ActionTimeoutError extends Error {
  constructor() {
    super('The service did not answer in time.');
    this.name = 'ActionTimeoutError';
  }
}

interface ErrorLike {
  readonly name?: unknown;
  readonly status?: unknown;
  readonly code?: unknown;
  readonly message?: unknown;
  readonly retryAfterSeconds?: unknown;
}

function asErrorLike(error: unknown): ErrorLike {
  return typeof error === 'object' && error !== null ? error as ErrorLike : {};
}

function serviceNote(error: ErrorLike): string {
  const message = typeof error.message === 'string' ? error.message.trim() : '';
  return message ? ` Service message: ${message}` : '';
}

/** Explains a failed mutation request. Anything that leaves the outcome
 *  uncertain (timeout, transport failure, server error) is `unknown_result`. */
export function describeActionError(error: unknown): { readonly failure: 'refused' | 'unknown_result' | 'error'; readonly message: string } {
  const like = asErrorLike(error);
  const copy = CONTROLS_PANEL_COPY;
  if (like.name === 'ActionTimeoutError') {
    return { failure: 'unknown_result', message: `${copy.resultUnknownTitle} ${copy.resultUnknown}` };
  }
  const status = typeof like.status === 'number' ? like.status : null;
  if (status === null) {
    return { failure: 'error', message: error instanceof Error && error.message ? error.message : copy.failedGeneric };
  }
  if (status === 0) return { failure: 'unknown_result', message: `${copy.resultUnknownTitle} ${copy.transportUnknown}` };
  if (status >= 500) return { failure: 'unknown_result', message: `${copy.resultUnknownTitle} ${copy.serverUnknown}` };
  const refused = (explanation: string, withNote = false) => ({
    failure: 'refused' as const,
    message: `${copy.refusedTitle} ${explanation}${withNote ? serviceNote(like) : ''}`,
  });
  if (status === 401) return refused(copy.refusedSession);
  if (status === 403) return refused(copy.refusedForbidden);
  if (status === 404) return refused(copy.refusedNotFound);
  if (status === 429) {
    const wait = typeof like.retryAfterSeconds === 'number' ? ` Retry in ${like.retryAfterSeconds} s.` : '';
    return refused(`${copy.refusedRateLimited}${wait}`);
  }
  if (status === 409) {
    if (like.code === 'action_in_progress') return refused(copy.refusedInProgress);
    if (like.code === 'wrong_confirmation') return refused(copy.refusedConfirmation);
    return refused(copy.refusedNotAvailable, true);
  }
  return refused(copy.refusedGeneric, true);
}

/** Explains a read-back that did not confirm the effect. */
export function describeVerifyError(error: unknown): string {
  const copy = CONTROLS_PANEL_COPY;
  if (error instanceof EffectNotConfirmedError) return `${copy.notConfirmedTitle} ${error.message}`;
  return `${copy.notConfirmedTitle} ${copy.notConfirmedRead}`;
}

function recordAt(value: unknown, key: string): unknown {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>)[key] : undefined;
}

/**
 * The declared state a single-pipeline action should leave behind: what the
 * service reported when it accepted the request, else what the preview
 * announced (`dry_run.would_transition.to`). `null` when neither exists, so
 * the read-back cannot pretend to compare.
 */
export function expectedDeclaredState(accepted: unknown, preview: unknown): string | null {
  const reported = recordAt(accepted, 'declaredState');
  if (typeof reported === 'string' && reported) return reported;
  const announced = recordAt(recordAt(recordAt(preview, 'dryRun'), 'would_transition'), 'to');
  return typeof announced === 'string' && announced ? announced : null;
}

export interface ScopeExpectation {
  readonly pipelineId: string;
  readonly declaredState: string;
}

/** Per-table states reported by a source/destination/fleet action
 *  (`after.pipelines.applied[]`); `null` when the service did not report them. */
export function scopeExpectations(accepted: unknown): readonly ScopeExpectation[] | null {
  const applied = recordAt(recordAt(accepted, 'pipelines'), 'applied');
  if (!Array.isArray(applied)) return null;
  const result: ScopeExpectation[] = [];
  for (const item of applied) {
    const pipelineId = recordAt(item, 'pipeline_id');
    const declaredState = recordAt(recordAt(item, 'after'), 'declared_state');
    if (typeof pipelineId !== 'string' || typeof declaredState !== 'string') return null;
    result.push({ pipelineId, declaredState });
  }
  return result;
}
