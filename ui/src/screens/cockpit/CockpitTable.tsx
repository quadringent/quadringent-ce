/**
 * Cockpit — Table : onglets Métriques, Dernières lignes, Journaux, Coûts,
 * Preuves (design §3, niveau table). ControlsPanel avec les cinq actions du
 * contrat (les seules qui existent à ce niveau : pause/resume sans
 * confirmation, remove/restart_initial_copy/replay sensibles).
 *
 * « Dernières lignes » n'affiche jamais de valeur de ligne (règle produit,
 * AGENTS.md) : faute d'un endpoint dédié dans le contrat v2 à ce jour, cet
 * onglet réutilise les journaux niveau `info` comme proxy de la dernière
 * activité observée (horodatage, message) — à remplacer si le contrat
 * publie un jour une route dédiée.
 */
import { useEffect, useState, type KeyboardEvent } from 'react';
import { StatusWord } from '../../components/foundation/StatusWord.tsx';
import { MetricChart } from '../../components/foundation/MetricChart.tsx';
import { FunctionKeyBar } from '../../components/foundation/FunctionKeyBar.tsx';
import { useFunctionKeys, type FunctionKeyBinding } from '../../components/foundation/useFunctionKeys.ts';
import { ControlsPanel } from '../../components/ControlsPanel.tsx';
import { declaredStateToStatusWord } from '../../domain/cockpit.ts';
import { getCockpitClient, useCockpitTable, type CockpitTableState } from '../../data/useCockpit.ts';
import { href, type CockpitTableTab } from '../../router.ts';
import type { ControlActionKind } from '../../domain/controlsPanel.ts';
import { ControlPlaneV2Error, pendingConfirmationId, type LogLevel } from '../../data/controlPlaneV2Client.ts';
import { focusPrimaryControls, navigateBack } from './cockpitFunctionKeys.ts';
import { EVIDENCE_COPY } from '../../domain/operator.ts';

const TABS: readonly { readonly id: CockpitTableTab; readonly label: string }[] = [
  { id: 'metrics', label: "Metrics" },
  { id: 'rows', label: "Latest rows" },
  { id: 'logs', label: "Logs" },
  { id: 'costs', label: "Costs" },
  { id: 'proofs', label: "Evidence" },
];

export function CockpitTableScreen({ pipelineId, tab }: { readonly pipelineId: string; readonly tab: CockpitTableTab }) {
  const [window, setWindow] = useState<'1h' | '24h'>('1h');
  const [level, setLevel] = useState<LogLevel | undefined>(undefined);
  const [correlateIncident, setCorrelateIncident] = useState(false);
  const { state, reload } = useCockpitTable(pipelineId, window, { level, correlateIncident });

  return (
    <CockpitTableView
      pipelineId={pipelineId}
      tab={tab}
      state={state}
      window={window}
      onWindowChange={setWindow}
      level={level}
      onLevelChange={setLevel}
      correlateIncident={correlateIncident}
      onCorrelateIncidentChange={setCorrelateIncident}
      onRefresh={reload}
    />
  );
}

export function CockpitTableView({
  pipelineId,
  tab,
  state,
  window,
  onWindowChange,
  level,
  onLevelChange,
  correlateIncident,
  onCorrelateIncidentChange,
  onRefresh,
}: {
  readonly pipelineId: string;
  readonly tab: CockpitTableTab;
  readonly state: CockpitTableState;
  readonly window: '1h' | '24h';
  readonly onWindowChange: (window: '1h' | '24h') => void;
  readonly level: LogLevel | undefined;
  readonly onLevelChange: (level: LogLevel | undefined) => void;
  readonly correlateIncident: boolean;
  readonly onCorrelateIncidentChange: (value: boolean) => void;
  readonly onRefresh: () => void;
}) {
  const activeTab = tab ?? 'metrics';
  const [focusedTab, setFocusedTab] = useState(activeTab);
  const displayName = state.status === 'ready' ? state.data.name ?? pipelineId : pipelineId;

  useEffect(() => { setFocusedTab(activeTab); }, [activeTab]);

  useEffect(() => {
    document.title = `Quadringent · Cockpit · ${displayName}`;
  }, [displayName]);

  const bindings: readonly FunctionKeyBinding[] = [
    { key: 'F3', onTrigger: navigateBack },
    { key: 'F5', onTrigger: onRefresh },
    { key: 'F9', onTrigger: focusPrimaryControls, disabled: state.status !== 'ready' },
    { key: 'F12', onTrigger: () => { globalThis.location.hash = href({ name: 'cockpit-table', id: pipelineId, tab: 'logs' }); } },
  ];
  const entries = useFunctionKeys(bindings);

  return (
    <div className="board cockpit-table">
      <header className="board__head">
        <div className="cockpit-table__identity">
          <p className="board__kicker"><a href={href({ name: 'cockpit' })}>Cockpit</a></p>
          <h1 className="board__title">{displayName}</h1>
          {displayName !== pipelineId ? <p className="cockpit-table__id mono">{pipelineId}</p> : null}
          {state.status === 'ready' ? <StatusWord state={declaredStateToStatusWord(state.data.pipeline.declaredState)} /> : null}
        </div>
        <button type="button" className="board__refresh" onClick={onRefresh}>Refresh</button>
      </header>

      {state.status === 'ready' && state.data.pipeline.attentionReason ? (
        <p role="status">{state.data.pipeline.attentionReason}</p>
      ) : null}

      <nav className="cockpit-table__tabs" aria-label="Table tabs">
        <ul role="tablist" onKeyDown={(event) => onTabsKeyDown(event, activeTab)}>
          {TABS.map((item) => (
            <li key={item.id} role="presentation">
              <a
                role="tab"
                id={`cockpit-table-tab-${item.id}`}
                className={activeTab === item.id ? 'is-current' : undefined}
                aria-selected={activeTab === item.id}
                tabIndex={focusedTab === item.id ? 0 : -1}
                onFocus={() => setFocusedTab(item.id)}
                aria-current={activeTab === item.id ? 'page' : undefined}
                href={href({ name: 'cockpit-table', id: pipelineId, tab: item.id })}
              >
                {item.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>

      {state.status === 'ready' ? (
        <div id="cockpit-primary-controls">
          <ControlsPanel
            level="table"
            targetLabel={pipelineId}
            availableActions={['pause', 'resume', 'restart_initial_copy', 'remove', 'replay'] as readonly ControlActionKind[]}
            dryRun={async (action) => {
              const client = await getCockpitClient();
              return client.runPipelineAction(pipelineId, action, { dryRun: true });
            }}
            execute={async (action, confirmationToken) => {
              const client = await getCockpitClient();
              try {
                await client.runPipelineAction(pipelineId, action, { confirmationToken });
                return { pendingConfirmationId: null };
              } catch (error) {
                if (error instanceof ControlPlaneV2Error) {
                  const confirmationId = pendingConfirmationId(error);
                  if (confirmationId) return { pendingConfirmationId: confirmationId };
                }
                throw error;
              }
            }}
            approveConfirmation={async (confirmationId) => {
              const client = await getCockpitClient();
              await client.approveConfirmation(confirmationId);
            }}
            rejectConfirmation={async (confirmationId) => {
              const client = await getCockpitClient();
              await client.rejectConfirmation(confirmationId);
            }}
            verify={async () => { onRefresh(); }}
          />
        </div>
      ) : null}

      {state.status === 'loading' ? (
        <section className="board__empty"><h2>Reading</h2><p>Quadringent is querying the service.</p></section>
      ) : state.status === 'failed' ? (
        <section className="board__empty" role="alert"><h2>Service unavailable</h2><p>{state.message}</p></section>
      ) : (
        <>
          {activeTab === 'metrics' ? (
            <MetricsTab data={state.data} window={window} onWindowChange={onWindowChange} />
          ) : null}
          {activeTab === 'rows' ? <RowsTab data={state.data} /> : null}
          {activeTab === 'logs' ? (
            <LogsTab data={state.data} level={level} onLevelChange={onLevelChange} correlateIncident={correlateIncident} onCorrelateIncidentChange={onCorrelateIncidentChange} />
          ) : null}
          {activeTab === 'costs' ? <CostsTab data={state.data} /> : null}
          {activeTab === 'proofs' ? <ProofsTab data={state.data} /> : null}
        </>
      )}

      <FunctionKeyBar entries={entries} onTrigger={(key) => bindings.find((binding) => binding.key === key)?.onTrigger()} />
    </div>
  );
}

/** Déplace le focus entre les onglets aux flèches gauche/droite (pattern
 *  ARIA tablist) — l'activation reste au clic/Entrée sur le lien ciblé,
 *  ces onglets restent de vraies navigations (deep-linkables), pas un
 *  panneau géré en mémoire. */
function onTabsKeyDown(event: KeyboardEvent<HTMLUListElement>, activeTab: CockpitTableTab): void {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  event.preventDefault();
  const focused = event.target instanceof Element ? event.target.closest('[role="tab"]') : null;
  const focusedIndex = TABS.findIndex((item) => focused?.id === `cockpit-table-tab-${item.id}`);
  const currentIndex = focusedIndex >= 0 ? focusedIndex : TABS.findIndex((item) => item.id === activeTab);
  const delta = event.key === 'ArrowRight' ? 1 : -1;
  const nextIndex = (currentIndex + delta + TABS.length) % TABS.length;
  const next = TABS[nextIndex]!;
  const target = event.currentTarget.querySelector<HTMLAnchorElement>(`#cockpit-table-tab-${next.id}`);
  target?.focus();
}

function MetricsTab({ data, window, onWindowChange }: { readonly data: import('../../data/useCockpit.ts').CockpitTableData; readonly window: '1h' | '24h'; readonly onWindowChange: (window: '1h' | '24h') => void }) {
  const fromLoader = data.metrics.provenance === 'journal_chargeur_kubernetes';
  return (
    <section className="cockpit-table__tab" aria-label="Metrics">
      <div className="cockpit-table__window-toggle">
        <button type="button" aria-pressed={window === '1h'} onClick={() => onWindowChange('1h')}>1 h</button>
        <button type="button" aria-pressed={window === '24h'} onClick={() => onWindowChange('24h')}>24 h</button>
      </div>
      <MetricChart label={fromLoader ? "IBM i → mirror delay" : "Lag"} values={data.metrics.points.map((point) => point.lagSeconds)} unit=" s" />
      <MetricChart label="Throughput" values={data.metrics.points.map((point) => point.throughputRowsPerSecond)} unit=" rows/s" />
      {data.metrics.freshness ? <p className="cockpit-table__note">Series {data.metrics.freshness}.</p> : null}
      {data.metrics.points.length === 0 && data.metrics.reason ? <p className="cockpit-table__note">{data.metrics.reason}</p> : null}
    </section>
  );
}

function RowsTab({ data }: { readonly data: import('../../data/useCockpit.ts').CockpitTableData }) {
  const activity = data.logs.filter((entry) => entry.level === 'info');
  if (activity.length === 0) {
    return <section className="cockpit-table__tab" aria-label="Latest rows"><p>No activity observed recently.</p></section>;
  }
  return (
    <section className="cockpit-table__tab" aria-label="Latest rows">
      <p className="cockpit-table__note">Observed activity — row values are never displayed here.</p>
      <ul className="cockpit-table__activity">
        {activity.map((entry, index) => (
          <li key={index}><time dateTime={entry.at}>{entry.at}</time> — {entry.message}</li>
        ))}
      </ul>
    </section>
  );
}

function LogsTab({
  data,
  level,
  onLevelChange,
  correlateIncident,
  onCorrelateIncidentChange,
}: {
  readonly data: import('../../data/useCockpit.ts').CockpitTableData;
  readonly level: LogLevel | undefined;
  readonly onLevelChange: (level: LogLevel | undefined) => void;
  readonly correlateIncident: boolean;
  readonly onCorrelateIncidentChange: (value: boolean) => void;
}) {
  return (
    <section className="cockpit-table__tab" aria-label="Logs">
      <div className="cockpit-table__log-filters">
        <label>
          Level
          <select value={level ?? ''} onChange={(event) => onLevelChange(event.target.value ? event.target.value as LogLevel : undefined)}>
            <option value="">All</option>
            <option value="info">Info</option>
            <option value="warning">Attention</option>
            <option value="error">Error</option>
          </select>
        </label>
        <label>
          <input type="checkbox" checked={correlateIncident} onChange={(event) => onCorrelateIncidentChange(event.target.checked)} />
          Incident-correlated only
        </label>
      </div>
      {data.logs.length === 0 ? (
        <p>No logs for this filter.</p>
      ) : (
        <ul className="cockpit-table__logs">
          {data.logs.map((entry, index) => (
            <li key={index} className={`cockpit-table__log-entry cockpit-table__log-entry--${entry.level}`}>
              <time dateTime={entry.at}>{entry.at}</time>
              <span>{entry.message}</span>
              {entry.incidentId ? <span className="cockpit-table__incident-tag">{entry.incidentId}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function CostsTab({ data }: { readonly data: import('../../data/useCockpit.ts').CockpitTableData }) {
  const cost = data.costToday;
  if (!cost || cost.status === 'absent' || cost.amount === null) {
    return <section className="cockpit-table__tab" aria-label="Costs"><p>Absent — no measured or estimated cost for this table.</p></section>;
  }
  return (
    <section className="cockpit-table__tab" aria-label="Costs">
      <p className="mono">
        {cost.amount.toFixed(2)} {cost.currency ?? ''}
        <span> · {cost.status === 'measured' ? "measured" : "estimated"}</span>
      </p>
      {cost.basis ? <p className="cockpit-table__cost-basis">Database: {cost.basis}</p> : null}
      {cost.collectedAt ? <p className="cockpit-table__cost-freshness">Observed on {cost.collectedAt}</p> : null}
    </section>
  );
}

function ProofsTab({ data }: { readonly data: import('../../data/useCockpit.ts').CockpitTableData }) {
  const evidence = data.evidence;
  const stages = evidence ? [
    ['Journal capture', evidence.reading],
    ['Snowflake delivery', evidence.delivery],
    ['Destination reconciliation', evidence.reconciliation],
  ] as const : [];
  return (
    <section className="cockpit-table__tab" aria-label="Evidence">
      {!evidence ? <p>{data.evidenceError ?? 'Absent — no delivery evidence available for this table.'}</p> : (
        <>
          <p className="cockpit-table__note">
            {evidence.evidenceKind === 'simulation' ? 'Demonstration — synthetic data' : `Evidence: ${evidence.evidenceKind}`}
            {' · '}{evidence.provenance}
            {evidence.collectedAt ? <> · Read at <time dateTime={evidence.collectedAt}>{evidence.collectedAt}</time></> : null}
          </p>
          <p className="cockpit-table__note">{EVIDENCE_COPY.producerVersion}: {evidence.producerVersion ?? EVIDENCE_COPY.versionMissing}</p>
          <div className="cockpit-table__evidence">
            {stages.map(([label, stage]) => (
              <article className="cockpit-table__evidence-stage" key={label}>
                <h2>{label}</h2>
                <p className="cockpit-table__note">{stage.state === 'absent' ? 'Not measured' : stage.state}</p>
                <dl>
                  <div><dt>Observed at</dt><dd>{stage.observedAt ? <time dateTime={stage.observedAt}>{stage.observedAt}</time> : '—'}</dd></div>
                  <div><dt>Events in measured scope</dt><dd>{stage.eventCount === null ? '—' : stage.eventCount.toLocaleString('en-US')}</dd></div>
                  <div><dt>Last batch delay</dt><dd>{stage.lagSeconds === null ? '—' : `${stage.lagSeconds} s`}</dd></div>
                  {label === 'Destination reconciliation' ? <div><dt>Duplicates in measured scope</dt><dd>{stage.duplicateCount === null ? '—' : stage.duplicateCount.toLocaleString('en-US')}</dd></div> : null}
                </dl>
                <p>{stage.detail}</p>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
