import { useEffect, useMemo, useState } from 'react';
import { ActionButton } from '../../components/foundation/ActionButton.tsx';
import { BandedTable, type BandedTableColumn } from '../../components/foundation/BandedTable.tsx';
import { useFunctionKeys } from '../../components/foundation/useFunctionKeys.ts';
import { FunctionKeyBar } from '../../components/foundation/FunctionKeyBar.tsx';
import { WIZARD_COPY } from '../../domain/operator.ts';
import { sequences } from '../../domain/format.ts';
import {
  evaluateTableReadiness,
  parseDiscoveryLibraries,
  isTableStartable,
  requiresKeyChoice,
  requiresRrnAcknowledgement,
  tableReadinessCopy,
  RRN_CONSEQUENCE_SENTENCE,
  type DiscoveredTable,
  type KeyStrategy,
  type TableReadiness,
} from '../../domain/wizardTables.ts';
import { ControlPlaneV2Error, type ControlPlaneV2Client } from '../../data/controlPlaneV2Client.ts';
import { WizardHeader, WizardPrimaryRow } from './WizardShell.tsx';

const copy = WIZARD_COPY.tables;

interface KeyChoice {
  readonly key: Exclude<KeyStrategy, 'primary'>;
  readonly columnsText: string;
  readonly acknowledged: boolean;
}

function parsedColumns(columnsText: string): readonly string[] {
  return columnsText.split(',').map((item) => item.trim()).filter((item) => item.length > 0);
}

export function WizardTables({
  client,
  sourceId: providedSourceId,
  destinationId = null,
  onBack,
  onStarted,
}: {
  readonly client: ControlPlaneV2Client;
  /** `null` quand l'assistant est ouvert directement sur cette étape (lien
   *  profond, rechargement) sans passer par « Source » dans cette session —
   *  résolu ici via `GET /v2/sources` plutôt que de bloquer l'écran. */
  readonly sourceId: string | null;
  readonly destinationId?: string | null;
  readonly onBack: () => void;
  readonly onStarted: (tableIds: readonly string[]) => void;
}) {
  const [resolvedSourceId, setResolvedSourceId] = useState<string | null>(providedSourceId);
  const [tables, setTables] = useState<readonly DiscoveredTable[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [search, setSearch] = useState('');
  const [librariesText, setLibrariesText] = useState('');
  const libraries = parseDiscoveryLibraries(librariesText);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [keyChoices, setKeyChoices] = useState<Readonly<Record<string, KeyChoice>>>({});
  const [status, setStatus] = useState<'idle' | 'loading' | 'starting' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);

  const load = async (refresh: boolean, sourceId: string) => {
    if (refresh && !libraries) { setError(copy.librariesInvalid); return; }
    setStatus('loading');
    setError(null);
    try {
      const items = refresh ? await client.refreshTables(sourceId, undefined, libraries ?? undefined) : await client.listTables(sourceId);
      setTables(items);
      setLoaded(true);
      setStatus('idle');
    } catch (cause) {
      setStatus('error');
      setError(cause instanceof ControlPlaneV2Error ? `${cause.message} ${cause.nextAction}` : "The table catalog is unavailable. Try again.");
    }
  };

  useEffect(() => {
    if (resolvedSourceId) return;
    let cancelled = false;
    void client.listSources().then((sources) => {
      if (cancelled) return;
      const first = sources[0];
      if (first) setResolvedSourceId(first.id);
      else setError("No IBM i source configured. Return to the previous stage.");
    }).catch(() => { if (!cancelled) setError("No IBM i source configured. Return to the previous stage."); });
    return () => { cancelled = true; };
  }, [client, resolvedSourceId]);

  useEffect(() => {
    if (!loaded && resolvedSourceId) void load(false, resolvedSourceId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedSourceId]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return tables;
    return tables.filter((table) => `${table.library}/${table.name}`.toLowerCase().includes(needle));
  }, [tables, search]);

  const toggleSelected = (id: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id); else next.delete(id);
      return next;
    });
  };

  const setKeyChoice = (id: string, key: Exclude<KeyStrategy, 'primary'>) => {
    setKeyChoices((current) => ({
      ...current,
      [id]: { key, columnsText: current[id]?.key === key ? current[id]!.columnsText : '', acknowledged: current[id]?.key === key ? current[id]!.acknowledged : false },
    }));
  };
  const setKeyColumnsText = (id: string, columnsText: string) => {
    setKeyChoices((current) => ({ ...current, [id]: { key: current[id]?.key ?? 'unique_index', columnsText, acknowledged: current[id]?.acknowledged ?? false } }));
  };
  const acknowledgeRrn = (id: string, acknowledged: boolean) => {
    setKeyChoices((current) => ({ ...current, [id]: { key: current[id]?.key ?? 'rrn', columnsText: current[id]?.columnsText ?? '', acknowledged } }));
  };

  const choiceFor = (table: DiscoveredTable) => {
    const raw = keyChoices[table.id];
    if (!raw) return undefined;
    return { key: raw.key, columns: parsedColumns(raw.columnsText), rrnAcknowledged: raw.acknowledged };
  };

  const isStartable = (table: DiscoveredTable): boolean => isTableStartable(table, choiceFor(table));

  const selectedStartable = [...selected].filter((id) => {
    const table = tables.find((item) => item.id === id);
    return table && isStartable(table);
  });

  const start = async () => {
    if (!destinationId) { setError(WIZARD_COPY.snowflake.destinationRequired); return; }
    setStatus('starting');
    setError(null);
    try {
      for (const id of selectedStartable) {
        const table = tables.find((item) => item.id === id)!;
        const choice = choiceFor(table);
        if (requiresKeyChoice(table) && choice) {
          await client.patchTableKey(id, { keyStrategy: choice.key, keyColumns: choice.columns, acknowledgeRrn: choice.rrnAcknowledged });
        }
        await client.startTablePipeline(id, { dryRun: false, destinationId });
      }
      setStatus('idle');
      onStarted(selectedStartable);
    } catch (cause) {
      setStatus('error');
      setError(cause instanceof ControlPlaneV2Error ? `${cause.message} ${cause.nextAction}` : "Starting failed for at least one table. Try again.");
    }
  };

  const bindings = [
    { key: 'F3' as const, onTrigger: onBack },
    { key: 'F5' as const, onTrigger: () => { if (resolvedSourceId) void load(true, resolvedSourceId); }, disabled: status === 'loading' || status === 'starting' || !resolvedSourceId || !libraries },
  ];
  const entries = useFunctionKeys(bindings);

  const columns: readonly BandedTableColumn<DiscoveredTable>[] = [
    {
      key: 'select', header: "Selection", render: (table) => (
        <span className="wizard__table-select">
          <input
            type="checkbox"
            aria-label={`Select ${table.library}/${table.name}`}
            checked={selected.has(table.id)}
            onChange={(event) => toggleSelected(table.id, event.target.checked)}
          />
        </span>
      ),
    },
    { key: 'name', header: 'Table', render: (table) => <span className="mono">{table.library}/{table.name}</span> },
    { key: 'rows', header: "Rows", numeric: true, render: (table) => sequences(table.approxRowCount) },
    { key: 'size', header: "Size", numeric: true, render: (table) => formatBytes(table.approxSizeBytes) },
    { key: 'readiness', header: "State", render: (table) => <ReadinessCell table={table} /> },
    {
      key: 'key', header: "Key", render: (table) => (
        requiresKeyChoice(table) ? (
          <KeyChoiceEditor
            table={table}
            choice={keyChoices[table.id]}
            onChoose={(key) => setKeyChoice(table.id, key)}
            onColumnsChange={(text) => setKeyColumnsText(table.id, text)}
            onAcknowledge={(ack) => acknowledgeRrn(table.id, ack)}
          />
        ) : <span className="sr-only">Key available</span>
      ),
    },
  ];

  const readyCount = tables.filter((table) => evaluateTableReadiness(table) === 'ready').length;
  const blockedTables = tables.filter((table) => {
    const readiness = evaluateTableReadiness(table);
    return readiness === 'not_journaled' || readiness === 'images_incomplete' || readiness === 'journal_mismatch';
  });

  return (
    <div className="wizard">
      <WizardHeader kicker={copy.kicker} title={copy.title} lead={copy.lead} />

      <p className="wizard__field">
        <label htmlFor="wizard-tables-libraries">{copy.libraries}</label>
        <input id="wizard-tables-libraries" className="wizard__search" type="text"
          value={librariesText} onChange={(event) => setLibrariesText(event.target.value)}
          aria-describedby="wizard-tables-libraries-hint" aria-invalid={librariesText.length > 0 && !libraries}
          disabled={status === 'loading' || status === 'starting'} />
        <span id="wizard-tables-libraries-hint" className="wizard__field-hint">{copy.librariesHint}</span>
        {librariesText.length > 0 && !libraries ? <span className="wizard__field-error">{copy.librariesInvalid}</span> : null}
      </p>

      <p className="wizard__field">
        <label htmlFor="wizard-tables-search">{copy.search}</label>
        <input id="wizard-tables-search" className="wizard__search" type="search" value={search} onChange={(event) => setSearch(event.target.value)} />
      </p>

      {error ? <p className="wizard__field-error" role="alert">{error}</p> : null}

      <div className="wizard__table-scroll">
        <BandedTable
          caption={`${copy.title} — ${readyCount}/${tables.length} ready`}
          columns={columns}
          rows={filtered}
          rowKey={(table) => table.id}
        />
      </div>

      {blockedTables.length > 0 ? (
        <section className="wizard__cl-panel" aria-label={copy.clPanelTitle}>
          <h2>{copy.clPanelTitle}</h2>
          {blockedTables.map((table) => (
            <div key={table.id}>
              <p className="mono">{table.library}/{table.name}</p>
              <pre>{table.clFixCommands.join('\n') || "No suggested command for this table."}</pre>
              <button
                type="button"
                className="action-button"
                onClick={() => { void navigator.clipboard.writeText(table.clFixCommands.join('\n')).catch(() => {}); }}
                disabled={table.clFixCommands.length === 0}
              >
                {WIZARD_COPY.snowflake.copy}
              </button>
            </div>
          ))}
          <ActionButton label={copy.recheck} onAction={() => { if (resolvedSourceId) void load(true, resolvedSourceId); }} disabled={status === 'loading' || status === 'starting' || !resolvedSourceId || !libraries} />
        </section>
      ) : null}

      {!destinationId ? <p role="status">{WIZARD_COPY.snowflake.destinationRequired}</p> : null}
      <WizardPrimaryRow>
        <ActionButton
          label={copy.start}
          onAction={() => { void start(); }}
          disabled={!destinationId || selectedStartable.length === 0 || status === 'starting'}
          requiresConfirmation
          primary
        />
      </WizardPrimaryRow>
      <FunctionKeyBar entries={entries} onTrigger={(key) => bindings.find((binding) => binding.key === key)?.onTrigger()} />
    </div>
  );
}

function ReadinessCell({ table }: { readonly table: DiscoveredTable }) {
  const readiness: TableReadiness = evaluateTableReadiness(table);
  const wordCopy = tableReadinessCopy(readiness);
  return (
    <span className={`wizard-check-line wizard-check-line--${readinessCheckState(readiness)}`}>
      <span aria-hidden="true" className="wizard-check-line__mark" />
      <span className="wizard-check-line__label">{wordCopy.label}</span>
      <span className="sr-only"> — {wordCopy.description}</span>
    </span>
  );
}

function readinessCheckState(readiness: TableReadiness): 'ok' | 'attention' | 'failed' {
  if (readiness === 'ready') return 'ok';
  if (readiness === 'no_key') return 'attention';
  return 'failed';
}

function KeyChoiceEditor({
  table,
  choice,
  onChoose,
  onColumnsChange,
  onAcknowledge,
}: {
  readonly table: DiscoveredTable;
  readonly choice: KeyChoice | undefined;
  readonly onChoose: (key: Exclude<KeyStrategy, 'primary'>) => void;
  readonly onColumnsChange: (columnsText: string) => void;
  readonly onAcknowledge: (acknowledged: boolean) => void;
}) {
  const groupName = `wizard-key-choice-${table.id}`;
  return (
    <fieldset className="wizard__key-choice">
      <legend className="sr-only">{copy.keyChoiceLabel} — {table.library}/{table.name}</legend>
      <label>
        <input type="radio" name={groupName} checked={choice?.key === 'unique_index'} onChange={() => onChoose('unique_index')} />
        {' '}{copy.keyChoiceUniqueIndex}
      </label>
      {choice?.key === 'unique_index' ? (
        <p className="wizard__field">
          <label htmlFor={`${groupName}-columns`}>{copy.keyColumnsLabel}</label>
          <input
            id={`${groupName}-columns`}
            type="text"
            value={choice.columnsText}
            onChange={(event) => onColumnsChange(event.target.value)}
            placeholder="CUSTOMER_ID, ORDER_ID"
          />
        </p>
      ) : null}
      <label>
        <input type="radio" name={groupName} checked={choice?.key === 'rrn'} onChange={() => onChoose('rrn')} />
        {' '}{copy.keyChoiceRrn}
      </label>
      {choice?.key && requiresRrnAcknowledgement(choice.key) ? (
        <>
          <p className="wizard__rrn-warning">{RRN_CONSEQUENCE_SENTENCE}</p>
          <label>
            <input type="checkbox" checked={choice.acknowledged} onChange={(event) => onAcknowledge(event.target.checked)} />
            {' '}{copy.rrnAcknowledge}
          </label>
        </>
      ) : null}
    </fieldset>
  );
}

function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 o';
  const units = ['o', "kB", "MB", "GB", 'To'];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}
