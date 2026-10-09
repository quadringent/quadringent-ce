import { useEffect, useRef, useState } from 'react';
import { ActionButton } from '../../components/foundation/ActionButton.tsx';
import { useFunctionKeys } from '../../components/foundation/useFunctionKeys.ts';
import { FunctionKeyBar } from '../../components/foundation/FunctionKeyBar.tsx';
import { WIZARD_COPY } from '../../domain/operator.ts';
import { validateSnowflakeAccount } from '../../domain/wizardSource.ts';
import { destinationInput, validateSnowflakeScope, retainDestinationSetup } from '../../domain/wizardSnowflake.ts';
import { ControlPlaneV2Error, type ControlPlaneV2Client, type DestinationRecord } from '../../data/controlPlaneV2Client.ts';
import { WizardHeader, WizardPrimaryRow } from './WizardShell.tsx';

const copy = WIZARD_COPY.snowflake;
type VerificationState = 'idle' | 'checking' | 'verified' | 'failed';

export function WizardSnowflake({
  client,
  existingDestination = null,
  destinations = [],
  loading = false,
  loadError = false,
  onRetry = () => {},
  onDestinationReady = () => {},
  onBack,
  onContinue,
}: {
  readonly client: ControlPlaneV2Client;
  readonly existingDestination?: DestinationRecord | null;
  readonly destinations?: readonly DestinationRecord[];
  readonly loading?: boolean;
  readonly loadError?: boolean;
  readonly onRetry?: () => void;
  readonly onDestinationReady?: (destination: DestinationRecord) => void;
  readonly onBack: () => void;
  readonly onContinue: (destinationId: string) => void;
}) {
  const [accountIdentifier, setAccountIdentifier] = useState(existingDestination?.accountIdentifier ?? '');
  const [destinationDatabase, setDestinationDatabase] = useState(existingDestination?.destinationDatabase ?? 'QUADRINGENT');
  const [destinationSchema, setDestinationSchema] = useState(existingDestination?.destinationSchema ?? '');
  const [touched, setTouched] = useState(false);
  const [destination, setDestination] = useState<DestinationRecord | null>(existingDestination);
  const [status, setStatus] = useState<'idle' | 'creating' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [verification, setVerification] = useState<VerificationState>('idle');
  const [verificationDetail, setVerificationDetail] = useState<string | null>(null);

  const mountedRef = useRef(false);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  const selectedIdRef = useRef(existingDestination?.id ?? null);
  selectedIdRef.current = existingDestination?.id ?? null;
  const [setupLoading, setSetupLoading] = useState(false);
  const [setupError, setSetupError] = useState(false);
  const [setupRetry, setSetupRetry] = useState(0);
  useEffect(() => {
    const selected = existingDestination;
    setDestination(selected);
    setAccountIdentifier(selected?.accountIdentifier ?? '');
    setDestinationDatabase(selected?.destinationDatabase ?? 'QUADRINGENT');
    setDestinationSchema(selected?.destinationSchema ?? '');
    setVerification('idle');
    setVerificationDetail(null);
    setSetupError(false);
    if (!selected) { setSetupLoading(false); return; }
    let cancelled = false;
    setSetupLoading(true);
    void client.getDestinationSetupScript(selected.id).then((sqlScript) => {
      if (!cancelled) setDestination({ ...selected, sqlScript });
    }).catch(() => { if (!cancelled) setSetupError(true); })
      .finally(() => { if (!cancelled) setSetupLoading(false); });
    return () => { cancelled = true; };
    // Une relecture du même ID ne réinitialise pas la sonde.
  }, [client, existingDestination?.id, setupRetry]);
  const resumeBlocked = loading || loadError || setupLoading || setupError
    || (destinations.length > 1 && !destination);

  const validation = validateSnowflakeAccount(accountIdentifier);
  const fieldError = touched && !validation.valid ? validation.message : null;
  const databaseValidation = validateSnowflakeScope(destinationDatabase, false);
  const schemaValidation = validateSnowflakeScope(destinationSchema, true);
  const formValid = validation.valid && databaseValidation.valid && schemaValidation.valid;

  const generateScript = async () => {
    if (!formValid || resumeBlocked || destination) return;
    setStatus('creating');
    setError(null);
    try {
      const created = await client.createDestination(destinationInput(accountIdentifier, destinationDatabase, destinationSchema));
      if (!mountedRef.current) return;
      setDestination(created);
      onDestinationReady(created);
      setStatus('idle');
    } catch (cause) {
      if (!mountedRef.current) return;
      setStatus('error');
      setError(cause instanceof ControlPlaneV2Error ? `${cause.message} ${cause.nextAction}` : "Script generation failed. Try again.");
    }
  };

  const copyScript = async () => {
    if (!destination) return;
    try {
      await navigator.clipboard.writeText(destination.sqlScript);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const verifyAccess = async () => {
    if (!destination || resumeBlocked || verification === 'checking') return;
    const selectedId = selectedIdRef.current;
    setVerification('checking');
    setVerificationDetail(null);
    try {
      const result = await client.verifyDestination(destination.id);
      if (!mountedRef.current || selectedIdRef.current !== selectedId) return;
      const reread = retainDestinationSetup(destination, result.destination);
      setDestination(reread);
      onDestinationReady(reread);
      setVerification(result.verified ? 'verified' : 'failed');
      setVerificationDetail(result.detail);
    } catch (cause) {
      if (!mountedRef.current || selectedIdRef.current !== selectedId) return;
      setVerification('failed');
      setVerificationDetail(cause instanceof ControlPlaneV2Error ? `${cause.message} ${cause.nextAction}` : null);
    }
  };

  const bindings = [
    { key: 'F3' as const, onTrigger: () => { if (status !== 'creating') onBack(); } },
  ];
  const entries = useFunctionKeys(bindings);

  return (
    <div className="wizard">
      <WizardHeader kicker={copy.kicker} title={copy.title} lead={copy.lead} />
      {loading ? <p role="status">{copy.resumeLoading}</p> : null}
      {loadError ? <p role="alert">{copy.resumeError} <button type="button" onClick={onRetry}>{copy.retry}</button></p> : null}
      {destinations.length > 1 ? <p className="wizard__field">
        <label htmlFor="wizard-destination-choice">{copy.destinationChoice}</label>
        <select id="wizard-destination-choice" value={destination?.id ?? ''} disabled={loading || loadError || setupLoading || verification === 'checking'}
          onChange={(event) => { const selected = destinations.find((item) => item.id === event.target.value); if (selected) onDestinationReady(selected); }}>
          <option value="" disabled>{copy.chooseDestination}</option>
          {destinations.map((item) => <option key={item.id} value={item.id}>{item.accountIdentifier} · {item.destinationDatabase ?? 'QUADRINGENT'}.{item.destinationSchema ?? 'RAW/CURATED'} · {item.id}</option>)}
        </select>
      </p> : null}
      {setupLoading ? <p role="status">{copy.setupLoading}</p> : null}
      {setupError ? <p role="alert">{copy.setupError} <button type="button" onClick={() => setSetupRetry((value) => value + 1)}>{copy.retry}</button></p> : null}
      {destination ? <p role="status">{destination.verificationState === 'verified' ? copy.persistedVerifiedState
        : destination.verificationState === 'failed' ? copy.persistedFailedState : copy.declaredState}</p> : null}
      <p className="wizard__field">
        <label htmlFor="wizard-snowflake-account">{copy.accountLabel}</label>
        <input
          id="wizard-snowflake-account"
          type="text"
          value={accountIdentifier}
          disabled={status === 'creating' || Boolean(destination)}
          aria-invalid={fieldError ? 'true' : undefined}
          aria-describedby={fieldError ? 'wizard-snowflake-account-error' : undefined}
          onChange={(event) => setAccountIdentifier(event.target.value)}
          onBlur={() => setTouched(true)}
        />
        {fieldError ? <span id="wizard-snowflake-account-error" className="wizard__field-error" role="alert">{fieldError}</span> : null}
      </p>

      {[
        { id: 'database', label: copy.databaseLabel, value: destinationDatabase, set: setDestinationDatabase, validation: databaseValidation },
        { id: 'schema', label: copy.schemaLabel, value: destinationSchema, set: setDestinationSchema, validation: schemaValidation },
      ].map((field) => (
        <p className="wizard__field" key={field.id}>
          <label htmlFor={`wizard-snowflake-${field.id}`}>{field.label}</label>
          <input id={`wizard-snowflake-${field.id}`} type="text" value={field.value}
            disabled={status === 'creating' || Boolean(destination)}
            aria-invalid={touched && !field.validation.valid ? 'true' : undefined}
            aria-describedby={`wizard-snowflake-${field.id}-hint`}
            onChange={(event) => field.set(event.target.value)} onBlur={() => setTouched(true)} />
          <span id={`wizard-snowflake-${field.id}-hint`} className={touched && !field.validation.valid ? 'wizard__field-error' : 'wizard__field-hint'}
            role={touched && !field.validation.valid ? 'alert' : undefined}>
            {touched && !field.validation.valid ? field.validation.message : field.id === 'schema' ? copy.schemaHint : null}
          </span>
        </p>
      ))}

      <ActionButton label="Generate script" onAction={() => { void generateScript(); }} disabled={resumeBlocked || !formValid || status === 'creating' || Boolean(destination)} />

      {error ? <p className="wizard__field-error" role="alert">{error}</p> : null}

      <SnowflakeDestinationReceipt destination={destination} copied={copied} onCopy={() => { void copyScript(); }}
        blocked={resumeBlocked} verification={verification} verificationDetail={verificationDetail} onVerify={() => { void verifyAccess(); }} onContinue={onContinue} />
      <FunctionKeyBar entries={entries} onTrigger={(key) => bindings.find((binding) => binding.key === key)?.onTrigger()} />
    </div>
  );
}

/** Affiche la réponse de création sans supposer que la clé unique est encore présente. */
export function SnowflakeDestinationReceipt({ destination, copied, onCopy, onContinue, verification = 'idle', verificationDetail = null, blocked = false, onVerify }: {
  readonly destination: DestinationRecord | null;
  readonly copied: boolean;
  readonly onCopy: () => void;
  readonly onContinue: (destinationId: string) => void;
  readonly blocked?: boolean;
  readonly verification?: VerificationState;
  readonly verificationDetail?: string | null;
  readonly onVerify: () => void;
}) {
  const scriptDownloadHref = destination
    ? `data:text/plain;charset=utf-8,${encodeURIComponent(destination.sqlScript)}`
    : undefined;
  // La clé privée n'est jamais relisible après la création (`DestinationsService.create`,
  // §`docs/api-v2.md`) : c'est la seule occasion de la récupérer.
  const keyDownloadHref = destination?.privateKeyPem
    ? `data:application/x-pem-file;charset=utf-8,${encodeURIComponent(destination.privateKeyPem)}`
    : undefined;

  const canContinue = Boolean(!blocked && destination && verification === 'verified' && destination.verificationState === 'verified');

  return (
    <>
      {destination ? (
        <>
          <h2>{copy.scriptTitle}</h2>
          <p>{copy.scriptLead}</p>
          <pre className="wizard__script">{destination.sqlScript}</pre>
          <div className="wizard__script-actions">
            <button type="button" className="action-button" onClick={() => { onCopy(); }}>{copy.copy}</button>
            <a className="action-button" href={scriptDownloadHref} download="quadringent-snowflake-setup.sql">{copy.download}</a>
            {keyDownloadHref ? (
              <a className="action-button" href={keyDownloadHref} download="quadringent-snowflake-private-key.pem">{copy.downloadKey}</a>
            ) : null}
          </div>
          {copied ? <p role="status">Script copied.</p> : null}

          {!keyDownloadHref ? <p className="wizard__field-hint" role="status">{copy.keyAlreadyIssued}</p> : null}
          <p className="wizard__field-hint">{keyDownloadHref ? copy.setupNext : copy.setupNextWithoutKey}</p>
          <p className="wizard__field-hint">{copy.verifyEffect}</p>
          <ActionButton label={verification === 'checking' ? copy.verifying : copy.verify}
            onAction={onVerify} disabled={blocked || verification === 'checking'} />
          {canContinue ? <p role="status">{copy.verified}</p> : null}
          {verification === 'failed' ? <p className="wizard__field-error" role="alert">{copy.verificationFailed}{verificationDetail ? ` ${verificationDetail}` : ''}</p> : null}
        </>
      ) : null}

      <WizardPrimaryRow>
        <ActionButton label={copy.primary} onAction={() => canContinue && destination && onContinue(destination.id)} disabled={!canContinue} primary />
      </WizardPrimaryRow>
    </>
  );
}
