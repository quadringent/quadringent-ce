/**
 * Installation — créer une liaison entre un AS400 et Snowflake.
 *
 * L'écran précédent était un formulaire de paramètres qui s'ouvrait sur les
 * réglages internes du lecteur et annonçait lui-même ne rien faire. Celui-ci
 * est un parcours : chaque page dit ce qu'elle demande, pourquoi, et ce qu'il
 * se passera ensuite. Les réglages du service restent du ressort du service.
 */

import { useEffect, useRef, useState } from 'react';
import { ControlPlaneClient } from '../data/controlPlaneClient.ts';
import type { ControlPlaneState } from '../data/useControlPlane.ts';
import { installedSiteIdentity } from '../domain/siteIdentity.ts';
import {
  buildOnboardingPayload,
  parseOnboardingVerdict,
  type OnboardingDraft,
  type OnboardingStep,
} from '../domain/onboarding.ts';
import {
  JOURNEY_COPY,
  PREREQUISITES,
  checkFrom,
  journeyMarks,
  nextStep,
  previousStep,
  serverStepFor,
  type JourneyCheck,
  type JourneyStep,
} from '../domain/journey.ts';
import { sequences } from '../domain/format.ts';
import { href } from '../router.ts';
import { connectionNetworkFailure } from '../domain/operator.ts';
import '../styles/journey.css';

const client = new ControlPlaneClient();

const emptyDraft: OnboardingDraft = {
  ibmiHost: '',
  ibmiUser: '',
  secretRefName: '',
  secretRefKey: '',
  schema: '',
  tables: [],
  journalLibrary: '',
  journalName: '',
  snowflakeDatabase: '',
  snowflakeSchema: '',
  snowflakeStage: '',
};

export function Setup({ state }: { readonly state: ControlPlaneState; readonly onRefresh?: () => void }) {
  const [step, setStep] = useState<JourneyStep>('preparer');
  const [draft, setDraft] = useState<OnboardingDraft>(emptyDraft);
  const [check, setCheck] = useState<JourneyCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [name, setName] = useState('');
  const heading = useRef<HTMLHeadingElement>(null);
  const site = installedSiteIdentity();

  useEffect(() => {
    heading.current?.focus();
  }, [step]);

  // Deux natures de champs, deux traitements.
  //
  // Ce que l'opérateur décide — l'adresse, le compte, la destination, les
  // tables — part vide : un formulaire pré-rempli avec une autre liaison
  // empêche de savoir si l'on consulte ou si l'on crée, et fait créer des
  // doublons par simple validation.
  //
  // Ce que la plateforme sait déjà — le suivi des modifications de la machine,
  // le schéma source, la zone de dépôt — est repris du service sans être
  // affiché. Le demander obligerait l'opérateur à connaître le modèle interne
  // de l'AS400 pour remplir un formulaire ; le taire ferait échouer la
  // création sur une exigence qu'il ne peut pas satisfaire.
  //
  // L'emplacement du suivi n'appartient ni au site publié ni au parcours : il
  // n'existe que dans le relevé d'une liaison déjà en service. On le reprend
  // quand il est là, et le champ replié reste la seule issue pour une machine
  // que Quadringent n'a encore jamais lue.
  // L'accès est défensif : ce parcours s'ouvre avant toute lecture, et un
  // relevé absent ne doit pas empêcher de le commencer.
  const observedJournalLibrary =
    state.status === 'loading' || state.status === 'failed'
      ? null
      : state.overview?.pipelines?.find((pipeline) => pipeline.flux?.journalLibrary)?.flux
          ?.journalLibrary ?? null;

  const hydrated = useRef(false);
  useEffect(() => {
    if (hydrated.current || site === null) return;
    hydrated.current = true;
    setDraft((current) => ({
      ...current,
      schema: site.sourceSchema,
      journalName: site.journalName,
      snowflakeStage: site.snowflakeStage,
    }));
  }, [site]);

  // L'identité du site est disponible avant le premier relevé : la reprise de
  // l'emplacement du suivi a donc son propre effet, qui attend que le relevé
  // arrive. Il ne réécrit jamais une valeur saisie à la main.
  useEffect(() => {
    if (observedJournalLibrary === null) return;
    setDraft((current) =>
      current.journalLibrary === ''
        ? { ...current, journalLibrary: observedJournalLibrary }
        : current,
    );
  }, [observedJournalLibrary]);

  const copy = JOURNEY_COPY[step];
  const marks = journeyMarks(step);

  if (created !== null) return <Created name={created} />;

  async function advance() {
    const serverStep = serverStepFor(step);
    const target = nextStep(step);
    setFailure(null);

    if (serverStep === null || site === null) {
      if (target) setStep(target);
      return;
    }

    setBusy(true);
    try {
      const payload = buildOnboardingPayload(draft, serverStep as OnboardingStep, site);

      // Dernière étape : on ne valide plus, on crée. Le service rejuge le
      // corps avec le même juge, donc un refus revient sous la même forme.
      if (target === null) {
        const outcome = await client.createConnection({ ...payload, display_name: name.trim() });
        if (outcome.ok) {
          setCheck(null);
          setCreated(name.trim() || draft.ibmiHost);
          return;
        }
        setCheck(checkFrom(parseOnboardingVerdict(outcome.verdict)));
        return;
      }

      const verdict = parseOnboardingVerdict(await client.evaluateOnboarding(payload));
      const result = checkFrom(verdict);
      setCheck(result);
      if (result.passed) setStep(target);
    } catch {
      // Une réponse perdue ne prouve pas que l'écriture a échoué.
      setFailure(
        target === null
          ? connectionNetworkFailure
          : "Verification did not complete: the service did not respond.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="journey">
      <header className="journey__head">
        <h1 className="journey__title" tabIndex={-1} ref={heading}>
          Connect an AS400 to Snowflake
        </h1>
        <ol className="journey__marks" aria-label="Progress">
          {marks.map((mark) => (
            <li key={mark.step} className={`journey__mark journey__mark--${mark.state}`}>
              <span className="journey__mark-label">{mark.label}</span>
            </li>
          ))}
        </ol>
      </header>

      <section className="journey__panel" aria-labelledby="journey-step-title">
        <h2 className="journey__step-title" id="journey-step-title">{copy.title}</h2>
        <p className="journey__why">{copy.why}</p>

        <div className="journey__body">
          {step === 'preparer' && <Prepare />}
          {step === 'source' && <SourceForm draft={draft} onChange={setDraft} />}
          {step === 'destination' && <DestinationForm draft={draft} onChange={setDraft} />}
          {step === 'donnees' && <TablesForm draft={draft} onChange={setDraft} site={site} />}
          {step === 'creer' && (
            <Recap draft={draft} pending={check?.pending ?? []} name={name} onName={setName} />
          )}
        </div>

        {check !== null && !check.passed && (
          <div className="journey__problems" role="alert">
            <p className="journey__problems-title">Fix before continuing</p>
            <ul>
              {check.problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </div>
        )}

        {failure !== null && (
          <p className="journey__failure" role="alert">{failure}</p>
        )}

        <footer className="journey__actions">
          {previousStep(step) !== null && (
            <button
              type="button"
              className="journey__back"
              onClick={() => { setCheck(null); setStep(previousStep(step)!); }}
            >
              Back
            </button>
          )}
          <button type="button" className="journey__next" onClick={() => void advance()} disabled={busy}>
            {busy ? "Verifying…" : copy.next}
          </button>
        </footer>
      </section>

      {state.status === 'failed' && (
        <p className="journey__offline">
          The service is not responding. You can complete the form, but creation will wait until it returns.
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Prepare() {
  return (
    <ul className="journey__checklist">
      {PREREQUISITES.map((item) => (
        <li key={item.label}>
          <p className="journey__checklist-label">{item.label}</p>
          <p className="journey__checklist-detail">{item.detail}</p>
        </li>
      ))}
    </ul>
  );
}

function Field({
  label,
  hint,
  value,
  placeholder,
  onChange,
}: {
  readonly label: string;
  readonly hint?: string;
  readonly value: string;
  readonly placeholder?: string;
  readonly onChange: (value: string) => void;
}) {
  const id = `field-${label.replace(/\s+/g, '-').toLowerCase()}`;
  return (
    <p className="journey__field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint !== undefined && <span className="journey__hint">{hint}</span>}
    </p>
  );
}

function SourceForm({
  draft,
  onChange,
}: {
  readonly draft: OnboardingDraft;
  readonly onChange: (draft: OnboardingDraft) => void;
}) {
  return (
    <>
      <Field
        label="Machine address"
        hint="An IP address or hostname."
        value={draft.ibmiHost}
        placeholder="192.0.2.10"
        onChange={(ibmiHost) => onChange({ ...draft, ibmiHost })}
      />
      <Field
        label="Reader account"
        hint="The account used to read your tables."
        value={draft.ibmiUser}
        onChange={(ibmiUser) => onChange({ ...draft, ibmiUser })}
      />
      <fieldset className="journey__group">
        <legend>Where the password is stored</legend>
        <p className="journey__group-why">
          Quadringent reads the password from your platform's vault. It never requests or displays it in this form.
        </p>
        <Field
          label="Secret name"
          value={draft.secretRefName}
          onChange={(secretRefName) => onChange({ ...draft, secretRefName })}
        />
        <Field
          label="Entry name"
          value={draft.secretRefKey}
          onChange={(secretRefKey) => onChange({ ...draft, secretRefKey })}
        />
      </fieldset>

      <details className="journey__advanced">
        <summary>Advanced settings</summary>
        <p className="journey__group-why">
          These two values describe where your AS400 records changes. Quadringent retrieves them from the service when already known; otherwise, your systems team will provide them.
        </p>
        <Field
          label="Change tracking location"
          value={draft.journalLibrary}
          placeholder="DEMOLIB"
          onChange={(journalLibrary) => onChange({ ...draft, journalLibrary })}
        />
        <Field
          label="Change tracking name"
          value={draft.journalName}
          onChange={(journalName) => onChange({ ...draft, journalName })}
        />
      </details>
    </>
  );
}

function DestinationForm({
  draft,
  onChange,
}: {
  readonly draft: OnboardingDraft;
  readonly onChange: (draft: OnboardingDraft) => void;
}) {
  return (
    <>
      <Field
        label="Snowflake database"
        value={draft.snowflakeDatabase}
        onChange={(snowflakeDatabase) => onChange({ ...draft, snowflakeDatabase })}
      />
      <Field
        label="Schema"
        hint="Your tables will be created here under their original names."
        value={draft.snowflakeSchema}
        onChange={(snowflakeSchema) => onChange({ ...draft, snowflakeSchema })}
      />
      {draft.snowflakeDatabase && draft.snowflakeSchema && (
        <p className="journey__preview">
          Your data will arrive in <strong>{draft.snowflakeDatabase}.{draft.snowflakeSchema}</strong>.
        </p>
      )}
    </>
  );
}

function TablesForm({
  draft,
  onChange,
  site,
}: {
  readonly draft: OnboardingDraft;
  readonly onChange: (draft: OnboardingDraft) => void;
  readonly site: ReturnType<typeof installedSiteIdentity>;
}) {
  const available = site?.manifest ?? [];
  if (available.length === 0) {
    return (
      <p className="journey__empty">
        The service has not published this machine's table list yet. It will appear here once the service reads it.
      </p>
    );
  }
  const selected = new Set(draft.tables);
  return (
    <>
      <p className="journey__count">
        {selected.size === 0
          ? `${sequences(available.length)} tables available`
          : `${sequences(selected.size)} out of ${sequences(available.length)} tables selected`}
      </p>
      <ul className="journey__tables">
        {available.map((table) => (
          <li key={table}>
            <label>
              <input
                type="checkbox"
                checked={selected.has(table)}
                onChange={(event) => {
                  const next = new Set(selected);
                  if (event.target.checked) next.add(table);
                  else next.delete(table);
                  onChange({ ...draft, tables: [...next] });
                }}
              />
              <span>{table}</span>
            </label>
          </li>
        ))}
      </ul>
      <p className="journey__hint">
        The first copy includes the full history and may take several hours for large tables. Subsequent changes arrive continuously.
      </p>
    </>
  );
}

function Recap({
  draft,
  pending,
  name,
  onName,
}: {
  readonly draft: OnboardingDraft;
  /** Ce que le parcours n'a pas pu éprouver — dit avant de créer, pas après. */
  readonly pending: readonly string[];
  readonly name: string;
  readonly onName: (value: string) => void;
}) {
  return (
    <>
      <Field
        label="Connection name"
        hint="The name shown in the list. Choose a descriptive name."
        value={name}
        placeholder="AS400 source"
        onChange={onName}
      />
      <dl className="journey__recap">
        <div>
          <dt>Machine</dt>
          <dd>{draft.ibmiHost || '—'}</dd>
        </div>
        <div>
          <dt>Account</dt>
          <dd>{draft.ibmiUser || '—'}</dd>
        </div>
        <div>
          <dt>Destination</dt>
          <dd>
            {draft.snowflakeDatabase && draft.snowflakeSchema
              ? `${draft.snowflakeDatabase}.${draft.snowflakeSchema}`
              : '—'}
          </dd>
        </div>
        <div>
          <dt>Tables</dt>
          <dd>{draft.tables.length > 0 ? sequences(draft.tables.length) : '—'}</dd>
        </div>
      </dl>
      <div className="journey__notice">
        <p>The connection will be saved with this information.</p>
        <ul className="journey__pending">
          {(pending.length > 0
            ? pending
            : ["The connection to your AS400 will be tested during activation."]
          ).map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <p>If anything is rejected at that point, the connection will show the reason.</p>
      </div>
      <p className="journey__hint">
        <a href={href({ name: 'overview' })}>Return to your connections</a>
      </p>
    </>
  );
}

/** Ce que l'opérateur voit une fois la liaison enregistrée. */
function Created({ name }: { readonly name: string }) {
  return (
    <div className="journey">
      <section className="journey__panel">
        <h1 className="journey__step-title">Connection saved</h1>
        <p className="journey__why">
          <strong>{name}</strong> now appears in your connections.
        </p>
        <div className="journey__notice">
          <p>
            It is not active yet: nobody is reading your AS400. Your operations team activates it, and the connection will be tested at that point.
          </p>
        </div>
        <footer className="journey__actions">
          <a className="journey__next" href={href({ name: 'overview' })}>
            View my connections
          </a>
        </footer>
      </section>
    </div>
  );
}
