import { useEffect, useRef, useState } from 'react';
import { FunctionKeyBar } from '../../components/foundation/FunctionKeyBar.tsx';
import { useFunctionKeys, type FunctionKeyBinding } from '../../components/foundation/useFunctionKeys.ts';
import type { ConfirmationRecord } from '../../data/controlPlaneV2Client.ts';
import { getCockpitClient } from '../../data/useCockpit.ts';
import { CONFIRMATIONS_COPY, confirmationActionLabel } from '../../domain/operator.ts';
import { href } from '../../router.ts';
import { navigateBack } from './cockpitFunctionKeys.ts';
import { DEFAULT_ACTION_TIMEOUT_MS, runWithTimeout } from '../../data/controlsPanelFlow.ts';
import { describeActionError, describeVerifyError, EffectNotConfirmedError } from '../../domain/controlsPanel.ts';

type InboxState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly items: readonly ConfirmationRecord[] }
  | { readonly status: 'failed'; readonly message: string };
type Decision = 'approve' | 'reject' | 'execute';
type Selection = { readonly record: ConfirmationRecord; readonly decision: Decision };

export function CockpitConfirmationsScreen() {
  const [state, setState] = useState<InboxState>({ status: 'loading' });
  const [nonce, setNonce] = useState(0);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [busy, setBusy] = useState(false);
  /** Synchronous guard: a double click must never send two decisions. */
  const deciding = useRef(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const reload = () => setNonce((value) => value + 1);

  useEffect(() => {
    document.title = 'Quadringent · Confirmations';
    let current = true;
    setState({ status: 'loading' });
    getCockpitClient()
      .then((client) => client.listConfirmations('all'))
      .then((items) => {
        if (current) setState({ status: 'ready', items: items.filter((item) => item.state === 'pending' || item.state === 'approved') });
      })
      .catch((error: unknown) => {
        if (current) setState({ status: 'failed', message: error instanceof Error ? error.message : String(error) });
      });
    return () => { current = false; };
  }, [nonce]);

  const decide = async () => {
    if (!selection || busy || deciding.current) return;
    deciding.current = true;
    setBusy(true);
    setFeedback(null);
    try {
      await runWithTimeout(() => performDecision(selection), DEFAULT_ACTION_TIMEOUT_MS);
    } catch (error: unknown) {
      setFeedback(decisionFailureMessage(error));
    } finally {
      deciding.current = false;
      setBusy(false);
      setSelection(null);
      reload();
    }
  };

  const performDecision = async (chosen: Selection): Promise<void> => {
    const client = await getCockpitClient();
    // The state may have changed since it was displayed: read the exact
    // capability again before the decision, then verify the effect.
    const fresh = await client.getConfirmation(chosen.record.id);
    const expectedState = chosen.decision === 'execute' ? 'approved' : 'pending';
    if (fresh.state !== expectedState || !fresh.available[chosen.decision]) {
      setFeedback(CONFIRMATIONS_COPY.changed);
      return;
    }
    if (chosen.decision === 'execute') {
      const action = fresh.actionRef === 'pipeline.remove' ? 'remove'
        : fresh.actionRef === 'pipeline.restart_initial_copy' ? 'restart_initial_copy' : null;
      if (!action || fresh.resourceType !== 'pipeline') throw new Error("This action cannot be executed from this screen.");
      const result = await client.runPipelineAction(fresh.resourceId, action, { confirmationToken: fresh.id });
      const targetState = action === 'remove' ? 'stopped' : 'copying';
      if (result.after?.declaredState !== targetState) throw new EffectNotConfirmedError("The service did not confirm the expected effect.");
      const verified = await client.getConfirmation(fresh.id);
      if (verified.state !== 'used') throw new EffectNotConfirmedError("The service did not confirm execution.");
      const pipeline = await client.getPipeline(fresh.resourceId);
      setFeedback(pipeline.attentionReason ?? result.after?.attentionReason ?? CONFIRMATIONS_COPY.executed);
      return;
    }
    if (chosen.decision === 'approve') await client.approveConfirmation(fresh.id);
    else await client.rejectConfirmation(fresh.id);
    const verified = await client.getConfirmation(fresh.id);
    const expected = chosen.decision === 'approve' ? 'approved' : 'rejected';
    if (verified.state !== expected) throw new EffectNotConfirmedError("The service did not confirm the decision.");
    setFeedback(chosen.decision === 'approve' ? CONFIRMATIONS_COPY.approved : CONFIRMATIONS_COPY.rejected);
  };

  return <CockpitConfirmationsView state={state} selection={selection} busy={busy} feedback={feedback}
    onRefresh={reload} onSelect={(record, decision) => setSelection({ record, decision })}
    onCancel={() => setSelection(null)} onDecide={decide} />;
}

export function CockpitConfirmationsView({
  state, selection = null, busy = false, feedback = null, onRefresh, onSelect, onCancel, onDecide,
}: {
  readonly state: InboxState;
  readonly selection?: Selection | null;
  readonly busy?: boolean;
  readonly feedback?: string | null;
  readonly onRefresh: () => void;
  readonly onSelect: (record: ConfirmationRecord, decision: Decision) => void;
  readonly onCancel: () => void;
  readonly onDecide: () => void;
}) {
  const bindings: readonly FunctionKeyBinding[] = [
    { key: 'F3', onTrigger: navigateBack },
    { key: 'F5', onTrigger: onRefresh },
    { key: 'F9', onTrigger: () => {}, disabled: true },
    { key: 'F12', onTrigger: () => {}, disabled: true },
  ];
  const entries = useFunctionKeys(bindings);

  return <div className="board cockpit-confirmations">
    <header className="board__head">
      <div>
        <p className="board__kicker"><a href={href({ name: 'cockpit' })}>Cockpit</a> / Decisions</p>
        <h1 className="board__title">{CONFIRMATIONS_COPY.title}</h1>
        <p className="board__summary">{CONFIRMATIONS_COPY.summary}</p>
      </div>
      <button type="button" className="board__refresh" onClick={onRefresh}>Refresh</button>
    </header>

    {feedback ? <p className="cockpit-confirmations__feedback" role="status">{feedback}</p> : null}
    {state.status === 'loading' ? (
      <section className="board__empty"><h2>Reading</h2><p>Quadringent is querying the service.</p></section>
    ) : state.status === 'failed' ? (
      <section className="board__empty" role="alert"><h2>Service unavailable</h2><p>{state.message}</p></section>
    ) : state.items.length === 0 ? (
      <section className="board__empty"><h2>{CONFIRMATIONS_COPY.empty}</h2><p>{CONFIRMATIONS_COPY.emptyDetail}</p></section>
    ) : (
      <ul className="cockpit-confirmations__list" aria-label="Confirmations to review">
        {state.items.map((record) => <li className="cockpit-confirmations__item" key={record.id}>
          <div className="cockpit-confirmations__main">
            <p className="cockpit-confirmations__eyebrow">{record.state === 'approved' ? "Approved" : "Pending request"}</p>
            <h2>{confirmationActionLabel(record.actionRef)}</h2>
            <p className="cockpit-confirmations__reason">{record.reason}</p>
            <p className="cockpit-confirmations__meta">
              {record.resourceType === 'pipeline'
                ? <a href={href({ name: 'cockpit-table', id: record.resourceId })}>Table {record.resourceId}</a>
                : <span>{record.resourceType} {record.resourceId}</span>}
              <span>Requested by {requester(record)}</span>
              {record.expiresAt ? <span>Expires on {formatDate(record.expiresAt)}</span> : null}
            </p>
            {record.riskEstimate ? <p className="cockpit-confirmations__risk">Reported risk: {record.riskEstimate}</p> : null}
            {record.state === 'approved' && !record.available.execute
              ? <p className="cockpit-confirmations__risk">{CONFIRMATIONS_COPY.executionUnavailable}</p> : null}
          </div>
          <div className="cockpit-confirmations__actions">
            {record.available.approve ? <button type="button" className="controls-panel__action controls-panel__action--primary"
              onClick={() => onSelect(record, 'approve')}>{CONFIRMATIONS_COPY.approve}</button> : null}
            {record.available.reject ? <button type="button" className="controls-panel__action controls-panel__action--destructive"
              onClick={() => onSelect(record, 'reject')}>{CONFIRMATIONS_COPY.reject}</button> : null}
            {record.available.execute ? <button type="button" className="controls-panel__action controls-panel__action--primary"
              onClick={() => onSelect(record, 'execute')}>{CONFIRMATIONS_COPY.execute}</button> : null}
          </div>
        </li>)}
      </ul>
    )}

    {selection ? <div className="cockpit-confirmations__overlay">
      <div className="cockpit-confirmations__dialog" role="dialog" aria-modal="true" aria-labelledby="confirmation-decision-title"
        onKeyDown={(event) => { if (event.key === 'Escape' && !busy) onCancel(); }}>
        <p className="cockpit-confirmations__eyebrow">Operator decision</p>
        <h2 id="confirmation-decision-title">{selection.decision === 'approve' ? CONFIRMATIONS_COPY.approveQuestion
          : selection.decision === 'reject' ? CONFIRMATIONS_COPY.rejectQuestion : CONFIRMATIONS_COPY.executeQuestion}</h2>
        <p>{confirmationActionLabel(selection.record.actionRef)} · {selection.record.resourceId}</p>
        <p>{selection.record.reason}</p>
        <div className="cockpit-confirmations__dialog-actions">
          <button type="button" className="controls-panel__cancel" onClick={onCancel} disabled={busy} autoFocus>{CONFIRMATIONS_COPY.cancel}</button>
          <button type="button" className={`controls-panel__action ${selection.decision === 'reject' ? 'controls-panel__action--destructive' : 'controls-panel__action--primary'}`}
            onClick={onDecide} disabled={busy}>
            {busy ? "Verification in progress…" : selection.decision === 'approve' ? CONFIRMATIONS_COPY.approveFinal
              : selection.decision === 'reject' ? CONFIRMATIONS_COPY.rejectFinal : CONFIRMATIONS_COPY.executeFinal}
          </button>
        </div>
      </div>
    </div> : null}
    <FunctionKeyBar entries={entries} onTrigger={(key) => bindings.find((binding) => binding.key === key)?.onTrigger()} />
  </div>;
}

/** A refusal, an unknown outcome (timeout, transport, server error) and an
 *  effect the read-back did not show are three different things. */
function decisionFailureMessage(error: unknown): string {
  if (error instanceof EffectNotConfirmedError) return describeVerifyError(error);
  const described = describeActionError(error);
  return described.message;
}

function requester(record: ConfirmationRecord): string {
  const kind = record.requestedByKind === 'agent' ? "the agent" : "an operator";
  return record.requestedById ? `${kind} ${record.requestedById}` : kind;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'date inconnue';
  return new Intl.DateTimeFormat("en-US", { hourCycle: 'h23', dateStyle: 'medium', timeStyle: 'short' }).format(date);
}
