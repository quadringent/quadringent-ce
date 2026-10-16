import { useEffect, useRef, useState } from 'react';
import type { CockpitTableData } from '../../data/cockpitLoaders.ts';
import { getCockpitClient } from '../../data/useCockpit.ts';
import { nativeVerificationError, runNativeVerification } from '../../data/nativeVerificationAction.ts';
import { NATIVE_VERIFICATION_COPY as COPY } from '../../domain/operator.ts';

export function NativeVerificationPanel({ data, onSettled }: { readonly data: CockpitTableData; readonly onSettled: () => void }) {
  const [record, setRecord] = useState(data.nativeVerification ?? null);
  const [phase, setPhase] = useState<'idle' | 'previewing' | 'confirm' | 'running' | 'done' | 'failed'>('idle');
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const restoreFocus = useRef(false);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => { setRecord(data.nativeVerification ?? null); }, [data.nativeVerification]);
  useEffect(() => () => { request.current?.abort(); request.current = null; }, []);
  const busy = phase === 'previewing' || phase === 'running';
  const cancel = () => { restoreFocus.current = true; setPhase('idle'); };
  useEffect(() => {
    if (phase === 'idle' && restoreFocus.current) {
      restoreFocus.current = false;
      button.current?.focus();
    }
  }, [phase]);

  async function perform(execute: boolean) {
    if (inFlight.current || !data.nativeCapabilities?.available) return;
    inFlight.current = true;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const timer = setTimeout(() => {
      controller.abort();
      if (request.current === controller) {
        setError(COPY.failed);
        setPhase('failed');
      }
    }, 60_000);
    setError(null);
    setPhase(execute ? 'running' : 'previewing');
    try {
      const client = await getCockpitClient();
      if (controller.signal.aborted) return;
      if (execute) {
        const result = await runNativeVerification(client, data.pipeline.id, controller.signal);
        if (controller.signal.aborted) return;
        setRecord(result);
        setPhase('done');
        onSettled();
      } else {
        if (!(await client.getNativeCapabilities(data.pipeline.id, controller.signal)).available) throw new Error();
        await client.requestNativeVerification(data.pipeline.id, true, controller.signal);
        if (!controller.signal.aborted) setPhase('confirm');
      }
    } catch (cause) {
      if (request.current === controller) { setError(nativeVerificationError(cause)); setPhase('failed'); }
    } finally {
      clearTimeout(timer);
      if (request.current === controller) inFlight.current = false;
    }
  }

  const result = record?.result;
  return (
    <section className="cockpit-table__tab native-verification" aria-label={COPY.title}>
      <div className="native-verification__head">
        <h2>{COPY.title}</h2>
        {data.nativeCapabilities?.available ? <button ref={button} type="button" className="controls-panel__action" disabled={busy || phase === 'confirm'} onClick={() => void perform(false)}>{COPY.run}</button> : null}
      </div>
      {data.nativeCapabilitiesError ? <p>{COPY.capabilityError}</p> : !data.nativeCapabilities?.available ? <p className="cockpit-table__note">{data.nativeCapabilities?.reasonCode === 'native_context_unavailable' ? COPY.contextUnavailable : COPY.unavailable}</p> : null}
      {busy ? <p role="status">{phase === 'previewing' ? COPY.previewing : COPY.executing}</p> : null}
      {phase === 'confirm' ? <div role="alertdialog" aria-label={COPY.confirmTitle} className="controls-panel__confirm" onKeyDown={event => { if (event.key === 'Escape') cancel(); }}>
        <p>{COPY.confirmDetail}</p>
        <div className="controls-panel__confirm-actions">
          <button autoFocus type="button" className="controls-panel__cancel" onClick={cancel}>{COPY.cancel}</button>
          <button type="button" className="controls-panel__action controls-panel__action--primary" onClick={() => void perform(true)}>{COPY.confirm}</button>
        </div>
      </div> : null}
      {error ? <p role="alert">{error}</p> : phase === 'done' ? <p role="status">{record?.result?.kind === 'admitted' ? COPY.finishedAdmitted : COPY.finished}</p> : null}
      {data.nativeVerificationError ? <p role="alert">{COPY.readError}</p> : null}
      {!record ? <p>{COPY.empty}</p> : <>
        <p><span className="evidence-state evidence-state--limit">{record.lifecycle === 'finished' ? result?.kind === 'admitted' ? COPY.admitted : COPY.incomplete : record.lifecycle === 'preparing' ? COPY.preparing : COPY.running}</span></p>
        <p className="cockpit-table__note">{result?.kind === 'observed' ? COPY.observed : result ? COPY.admitted : COPY.noAdmission}</p>
        <dl className="cockpit-table__evidence-facts">
          <div><dt>{COPY.resultId}</dt><dd>{record.id}</dd></div>
          <div><dt>{record.observedAt ? COPY.observedAt : COPY.createdAt}</dt><dd><time dateTime={record.observedAt ?? record.createdAt}>{record.observedAt ?? record.createdAt}</time></dd></div>
        </dl>
        <p className="native-verification__context">{COPY.contexts[record.contextValidity]}{record.contextValidity !== 'same_context' ? ` · ${COPY.contextCaveat}` : ''}</p>
        {result ? <>
          <p><strong>{result.comparisonState === 'divergent' ? COPY.divergent : COPY.equality}</strong></p>
          {result.comparisonState === 'divergent' ? <p>{COPY.differenceDetail}</p> : null}
          <div className="native-verification__tables">
            <table><caption>{COPY.layers}</caption><thead><tr><th scope="col">{COPY.layers}</th><th scope="col">{COPY.rows}</th><th scope="col">{COPY.duplicates}</th><th scope="col">{COPY.completedAt}</th></tr></thead>
              <tbody>{result.datasets.map(row => <tr key={row.name}><th scope="row">{COPY.datasets[row.name]}</th><td>{row.rowCount.toLocaleString('en-US')}</td><td>{row.duplicateCount.toLocaleString('en-US')}</td><td><time dateTime={row.completedAt}>{row.completedAt}</time></td></tr>)}</tbody>
            </table>
            <table><caption>{COPY.comparison}</caption><thead><tr><th scope="col">{COPY.comparison}</th><th scope="col">{COPY.missing}</th><th scope="col">{COPY.unexpected}</th><th scope="col">{COPY.different}</th></tr></thead>
              <tbody>{result.comparisons.map(row => <tr key={row.name}><th scope="row">{COPY.comparisonNames[row.name as keyof typeof COPY.comparisonNames]}<span className="native-verification__row-state">{row.equal ? COPY.matched : COPY.mismatch}</span></th><td>{row.missing}</td><td>{row.unexpected}</td><td>{row.different}</td></tr>)}</tbody>
            </table>
          </div>
          <details><summary>{COPY.validatedChecks}</summary><ul>{result.checks.map(check => <li key={check.name}>{COPY.checkNames[check.name as keyof typeof COPY.checkNames]} · {check.passed ? COPY.checked : COPY.notEstablished}</li>)}</ul></details>
          {result.kind === 'observed' ? <div className="native-verification__authorities">
            <div><h3>{COPY.observations}</h3>{result.validatedObservations.length ? <ul>{result.validatedObservations.map(name => <li key={name}>{COPY.observationNames[name as keyof typeof COPY.observationNames]}</li>)}</ul> : <p>{COPY.noObservations}</p>}</div>
            <div><h3>{COPY.missingAuthorities}</h3><ul>{result.missingAuthorities.map(name => <li key={name}>{COPY.authorityNames[name as keyof typeof COPY.authorityNames]}</li>)}</ul></div>
          </div> : null}
        </> : null}
      </>}
    </section>
  );
}
