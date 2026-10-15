import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ActionTimeoutError,
  describeActionError,
  describeVerifyError,
  EffectNotConfirmedError,
  expectedDeclaredState,
  scopeResult,
  availableTableActions,
  controlsPanelReducer,
  INITIAL_CONTROLS_PANEL_STATE,
  requiresConfirmation,
  type ControlsPanelState,
} from './controlsPanel.ts';

function apply(events: Parameters<typeof controlsPanelReducer>[1][], initial: ControlsPanelState = INITIAL_CONTROLS_PANEL_STATE): ControlsPanelState {
  return events.reduce(controlsPanelReducer, initial);
}

test('a non-sensitive action goes preview -> confirm -> run -> verify -> succeeded', () => {
  const state = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_ready', preview: { effect: 'Suspend la table.' } },
    { type: 'confirm' },
    { type: 'run_succeeded' },
    { type: 'verified' },
  ]);
  assert.equal(state.phase, 'succeeded');
  assert.equal(state.action, 'pause');
});

test('remove/restart_initial_copy/replay are flagged sensitive; pause/resume are not', () => {
  assert.equal(requiresConfirmation('remove'), true);
  assert.equal(requiresConfirmation('restart_initial_copy'), true);
  assert.equal(requiresConfirmation('replay'), true);
  assert.equal(requiresConfirmation('pause'), false);
  assert.equal(requiresConfirmation('resume'), false);
});

test('a sensitive action can come back pending_confirmation_required and waits for approval before rerunning', () => {
  const pending = apply([
    { type: 'request', action: 'remove' },
    { type: 'preview_ready', preview: { effect: 'Retire la table du suivi.' } },
    { type: 'confirm' },
    { type: 'run_requires_confirmation', confirmationId: 'cf_1' },
  ]);
  assert.equal(pending.phase, 'awaiting_confirmation');
  assert.equal(pending.confirmationState, 'pending');

  // confirming again before approval must not start the run
  const stillWaiting = controlsPanelReducer(pending, { type: 'confirm' });
  assert.equal(stillWaiting.phase, 'awaiting_confirmation');

  const approved = controlsPanelReducer(pending, { type: 'confirmation_approved' });
  assert.equal(approved.confirmationState, 'approved');
  assert.equal(approved.phase, 'awaiting_confirmation', 'approval alone never executes the action');

  const running = controlsPanelReducer(approved, { type: 'confirm' });
  assert.equal(running.phase, 'running');

  const done = apply([{ type: 'run_succeeded' }, { type: 'verified' }], running);
  assert.equal(done.phase, 'succeeded');
});

test('a rejected confirmation fails the panel with an explicit reason, never a silent no-op', () => {
  const pending = apply([
    { type: 'request', action: 'replay' },
    { type: 'preview_ready', preview: {} },
    { type: 'confirm' },
    { type: 'run_requires_confirmation', confirmationId: 'cf_2' },
  ]);
  const rejected = controlsPanelReducer(pending, { type: 'confirmation_rejected' });
  assert.equal(rejected.phase, 'failed');
  assert.equal(rejected.confirmationState, 'rejected');
  assert.match(rejected.error ?? '', /rejected/);
});

test('a failed dry_run never reaches running, and cancel from preview_ready returns to idle without residue', () => {
  const failed = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_failed', error: "Aperçu unavailable." },
  ]);
  assert.equal(failed.phase, 'failed');
  assert.equal(failed.error, "Aperçu unavailable.");

  const cancelled = apply([
    { type: 'request', action: 'resume' },
    { type: 'preview_ready', preview: {} },
    { type: 'cancel' },
  ]);
  assert.deepEqual(cancelled, INITIAL_CONTROLS_PANEL_STATE);
});

test('a verify failure after a successful run still reports failed, never a silent success', () => {
  const state = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_ready', preview: {} },
    { type: 'confirm' },
    { type: 'run_succeeded' },
    { type: 'verify_failed', error: 'Relecture impossible : nouvel état non confirmé.' },
  ]);
  assert.equal(state.phase, 'failed');
  assert.match(state.error ?? '', /Relecture impossible/);
});

test('out-of-phase events are ignored (no phantom transitions), e.g. run_succeeded while idle', () => {
  const state = controlsPanelReducer(INITIAL_CONTROLS_PANEL_STATE, { type: 'run_succeeded' });
  assert.deepEqual(state, INITIAL_CONTROLS_PANEL_STATE);
});

test('a new request while idle, succeeded or failed starts a clean cycle; not while busy', () => {
  const afterSuccess = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_ready', preview: {} },
    { type: 'confirm' },
    { type: 'run_succeeded' },
    { type: 'verified' },
    { type: 'request', action: 'resume' },
  ]);
  assert.equal(afterSuccess.phase, 'previewing');
  assert.equal(afterSuccess.action, 'resume');

  const whileRunning = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_ready', preview: {} },
    { type: 'confirm' },
    { type: 'request', action: 'resume' },
  ]);
  assert.equal(whileRunning.phase, 'running', 'a request mid-flight must not interrupt the running action');
  assert.equal(whileRunning.action, 'pause');
});

const RUNNING: ControlsPanelState = apply([
  { type: 'request', action: 'pause' },
  { type: 'preview_ready', preview: {} },
  { type: 'confirm' },
]);

test('failure kinds are recorded: refused, unknown result, not confirmed, rejected', () => {
  assert.equal(controlsPanelReducer(RUNNING, { type: 'run_failed', error: 'x', failure: 'refused' }).failure, 'refused');
  assert.equal(controlsPanelReducer(RUNNING, { type: 'run_failed', error: 'x', failure: 'unknown_result' }).failure, 'unknown_result');
  assert.equal(controlsPanelReducer(RUNNING, { type: 'run_failed', error: 'x' }).failure, 'error');
  const verifying = controlsPanelReducer(RUNNING, { type: 'run_succeeded' });
  assert.equal(controlsPanelReducer(verifying, { type: 'verify_failed', error: 'x' }).failure, 'not_confirmed');
  const pending = apply([{ type: 'run_requires_confirmation', confirmationId: 'cf_1' }], RUNNING);
  assert.equal(controlsPanelReducer(pending, { type: 'confirmation_rejected' }).failure, 'rejected');
});

test('an accepted request is verifying, never succeeded, until the read-back confirms it', () => {
  const accepted = controlsPanelReducer(RUNNING, { type: 'run_succeeded' });
  assert.equal(accepted.phase, 'verifying');
  assert.notEqual(accepted.phase, 'succeeded');
  assert.equal(controlsPanelReducer(accepted, { type: 'verified' }).phase, 'succeeded');
  const failed = controlsPanelReducer(accepted, { type: 'verify_failed', error: 'Effect not confirmed.' });
  assert.equal(failed.phase, 'failed');
  assert.notEqual(failed.phase, 'succeeded');
});

test('after an unknown result the only way forward is reading again, never a second mutation', () => {
  const unknown = controlsPanelReducer(RUNNING, { type: 'run_failed', error: 'timeout', failure: 'unknown_result' });
  const checking = controlsPanelReducer(unknown, { type: 'check_state' });
  assert.equal(checking.phase, 'verifying');
  assert.equal(checking.action, 'pause');
  // A refused failure offers no such path.
  const refused = controlsPanelReducer(RUNNING, { type: 'run_failed', error: 'no', failure: 'refused' });
  assert.equal(controlsPanelReducer(refused, { type: 'check_state' }), refused);
  assert.deepEqual(controlsPanelReducer(INITIAL_CONTROLS_PANEL_STATE, { type: 'check_state' }), INITIAL_CONTROLS_PANEL_STATE);
});

test('a failed approval keeps the panel on the pending confirmation and says why', () => {
  const pending = apply([{ type: 'run_requires_confirmation', confirmationId: 'cf_1' }], RUNNING);
  const failed = controlsPanelReducer(pending, { type: 'approval_failed', error: 'Your role does not allow this.' });
  assert.equal(failed.phase, 'awaiting_confirmation');
  assert.equal(failed.confirmationState, 'pending');
  assert.equal(failed.error, 'Your role does not allow this.');
  assert.equal(controlsPanelReducer(INITIAL_CONTROLS_PANEL_STATE, { type: 'approval_failed', error: 'x' }).error, null);
});

test('errors are explained by kind: refused statuses, unknown outcomes and unexpected failures', () => {
  const status = (value: number, extra: Record<string, unknown> = {}) => describeActionError(Object.assign(new Error('service text'), { status: value, ...extra }));
  assert.equal(status(403).failure, 'refused');
  assert.match(status(403).message, /Action refused\..*role does not allow/);
  assert.match(status(409, { code: 'capability_unavailable' }).message, /not possible in the current state/);
  assert.match(status(409, { code: 'action_in_progress' }).message, /already in progress/);
  assert.match(status(409, { code: 'wrong_confirmation' }).message, /confirmation is missing, expired or not approved/);
  assert.match(status(404).message, /no longer exists/);
  assert.match(status(401).message, /session has ended/);
  assert.match(status(429, { retryAfterSeconds: 7 }).message, /Too many requests.*Retry in 7 s\./);
  assert.equal(status(400).failure, 'refused');
});

test('a refusal never repeats the service text, which may name an internal event or transition', () => {
  const raw = "transition interdite : 'live' --resume_live--> ?";
  for (const code of ['capability_unavailable', 'invalid_request', 'internal_error']) {
    for (const status of [400, 409]) {
      const described = describeActionError(Object.assign(new Error(raw), { status, code }));
      assert.equal(described.failure, 'refused');
      assert.doesNotMatch(described.message, /resume_live|transition interdite|--/);
    }
  }
});

test('a 2xx answer that cannot be used is an unknown result, never "refused, nothing was changed"', () => {
  for (const error of [
    Object.assign(new Error('Unreadable response'), { status: 200, code: 'internal_error' }),
    Object.assign(new Error('bad'), { status: 200, code: 'invalid_response' }),
    Object.assign(new Error('bad'), { status: 204, code: 'invalid_response' }),
  ]) {
    const described = describeActionError(error);
    assert.equal(described.failure, 'unknown_result');
    assert.match(described.message, /Result unknown\./);
    assert.match(described.message, /may have been applied/);
    assert.doesNotMatch(described.message, /nothing was changed|refused/i);
  }
});

test('only the actions the declared-state machine allows are offered for a table', () => {
  assert.deepEqual(availableTableActions('live'), ['pause', 'restart_initial_copy', 'remove', 'replay']);
  assert.deepEqual(availableTableActions('copying'), ['pause', 'restart_initial_copy', 'remove', 'replay']);
  assert.deepEqual(availableTableActions('paused'), ['resume', 'restart_initial_copy', 'remove', 'replay']);
  assert.deepEqual(availableTableActions('attention'), ['resume', 'restart_initial_copy', 'remove', 'replay']);
  assert.deepEqual(availableTableActions('stopped'), []);
  assert.deepEqual(availableTableActions('not_started'), []);
  assert.ok(!availableTableActions('live').includes('resume'), 'resume only follows a pause or an attention');
  assert.ok(!availableTableActions('paused').includes('pause'));
  assert.deepEqual(availableTableActions('unexpected' as never), []);
});

test('verification success records what was confirmed, and an unchanged state is never a verification', () => {
  const verifying = controlsPanelReducer(RUNNING, { type: 'run_succeeded' });
  const done = controlsPanelReducer(verifying, { type: 'verified', confirmed: 'paused' });
  assert.equal(done.phase, 'succeeded');
  assert.equal(done.confirmed, 'paused');
  assert.equal(controlsPanelReducer(verifying, { type: 'verified' }).confirmed, null);
  assert.equal(controlsPanelReducer(done, { type: 'request', action: 'resume' }).confirmed, null);
});

test('a timeout, a transport failure and a server error are unknown results, not failures to retry', () => {
  for (const error of [new ActionTimeoutError(), Object.assign(new Error('x'), { status: 0 }), Object.assign(new Error('x'), { status: 502 }), Object.assign(new Error('x'), { status: 503, code: 'executor_unavailable' })]) {
    const described = describeActionError(error);
    assert.equal(described.failure, 'unknown_result');
    assert.match(described.message, /Result unknown\./);
    assert.match(described.message, /Check the current state before retrying/);
  }
  assert.match(describeActionError(new ActionTimeoutError()).message, /nothing was retried automatically/);
});

test('an unexpected error keeps its own message, or the generic one', () => {
  assert.deepEqual(describeActionError(new Error('Action not supported at fleet level.')), { failure: 'error', message: 'Action not supported at fleet level.' });
  assert.deepEqual(describeActionError('boom'), { failure: 'error', message: 'Action not executed.' });
});

test('a read-back that did not confirm is worded as "Effect not confirmed"', () => {
  assert.match(describeVerifyError(new EffectNotConfirmedError('The service reports "live" while "paused" was expected.')), /^Effect not confirmed\. The service reports "live"/);
  assert.match(describeVerifyError(new Error('network')), /^Effect not confirmed\..*could not be read again/);
  assert.match(describeVerifyError(new ActionTimeoutError()), /^Effect not confirmed\./);
});

test('the expected state comes from the accepted request first, then from the preview, else it is unknown', () => {
  assert.equal(expectedDeclaredState({ id: 'pl_1', declaredState: 'paused' }, null), 'paused');
  assert.equal(expectedDeclaredState(null, { dryRun: { would_transition: { from: 'live', to: 'paused' } } }), 'paused');
  assert.equal(expectedDeclaredState({ declaredState: 'live' }, { dryRun: { would_transition: { to: 'paused' } } }), 'live');
  assert.equal(expectedDeclaredState(null, { dryRun: { action: 'pause' } }), null);
  assert.equal(expectedDeclaredState(null, null), null);
});

test('a scope answer is read as the tables changed with their states and the tables skipped', () => {
  const accepted = { pipelines: { applied: [
    { pipeline_id: 'pl_1', before: { declared_state: 'live' }, after: { declared_state: 'paused' } },
    { pipeline_id: 'pl_2', before: { declared_state: 'copying' }, after: { declared_state: 'paused' } },
  ], skipped: [{ pipeline_id: 'pl_3', reason: "transition interdite : 'attention' --pause--> ?" }] } };
  assert.deepEqual(scopeResult(accepted), {
    applied: [{ pipelineId: 'pl_1', declaredState: 'paused' }, { pipelineId: 'pl_2', declaredState: 'paused' }],
    skippedIds: ['pl_3'],
  });
});

test('a scope answer that applied nothing is reported as such, not as an empty success', () => {
  assert.deepEqual(scopeResult({ pipelines: { applied: [], skipped: [] } }), { applied: [], skippedIds: [] });
  assert.deepEqual(scopeResult({ pipelines: { applied: [], skipped: [{ pipeline_id: 'pl_a', reason: 'x' }, { reason: 'no id' }] } }),
    { applied: [], skippedIds: ['pl_a', ''] });
});

test('a scope answer without the per-table report cannot be interpreted', () => {
  assert.equal(scopeResult({ affected: ['pl_1'] }), null);
  assert.equal(scopeResult({ pipelines: { applied: [{ pipeline_id: 'pl_1' }] } }), null);
  assert.equal(scopeResult({ pipelines: { applied: [] } }), null, 'skipped must be reported too');
  assert.equal(scopeResult(null), null);
});
