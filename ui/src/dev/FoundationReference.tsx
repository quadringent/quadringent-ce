import { useEffect, useState } from 'react';
import { ActionButton } from '../components/foundation/ActionButton.tsx';
import { BandedTable, type BandedTableColumn } from '../components/foundation/BandedTable.tsx';
import { FunctionKeyBar } from '../components/foundation/FunctionKeyBar.tsx';
import { useFunctionKeys } from '../components/foundation/useFunctionKeys.ts';
import { MetricTile } from '../components/foundation/MetricTile.tsx';
import { StatusWord, type StatusWordState } from '../components/foundation/StatusWord.tsx';

/**
 * Page de référence visuelle, atteignable uniquement en développement
 * (`import.meta.env.DEV`, voir `src/main.tsx`) via `#/_fondation`. Jamais
 * incluse dans le contrat produit ni dans le build de production : elle sert
 * la revue du product owner sur les cinq composants de fondation, en clair
 * et en sombre, à la largeur de travail desktop (1440 px).
 */

const STATUS_STATES: readonly StatusWordState[] = ['live', 'copying', 'paused', 'attention', 'stopped'];

interface TableRow {
  readonly id: string;
  readonly library: string;
  readonly table: string;
  readonly rows: number;
}

const TABLE_ROWS: readonly TableRow[] = [
  { id: '1', library: 'QGPL', table: "CUSTOMER", rows: 1_204 },
  { id: '2', library: 'QGPL', table: "ORDER", rows: 88_031 },
  { id: '3', library: 'QGPL', table: "ORDER_LINE", rows: 412_558 },
  { id: '4', library: "INVOICE", table: "HEADER", rows: 22_910 },
  { id: '5', library: "INVOICE", table: 'DETAIL', rows: 190_442 },
];

const columns: readonly BandedTableColumn<TableRow>[] = [
  { key: 'library', header: "Library", render: (row) => <span className="mono">{row.library}</span> },
  { key: 'table', header: 'Table', render: (row) => <span className="mono">{row.table}</span> },
  { key: 'rows', header: "Rows", numeric: true, render: (row) => row.rows.toLocaleString("en-US") },
];

export function FoundationReference() {
  const [theme, setTheme] = useState<'light' | 'dark'>('light');
  const [log, setLog] = useState<string[]>([]);
  const pushLog = (message: string) => setLog((previous) => [message, ...previous].slice(0, 6));

  const functionKeyBindings = [
    { key: 'F3' as const, onTrigger: () => pushLog("F3 — Go back") },
    { key: 'F5' as const, onTrigger: () => pushLog("F5 — Refresh") },
    { key: 'F9' as const, onTrigger: () => pushLog("F9 — Pause/Resume") },
    { key: 'F12' as const, onTrigger: () => pushLog("F12 — Logs"), disabled: true },
  ];
  const entries = useFunctionKeys(functionKeyBindings);

  // Les jetons de thème sont scopés à :root ; le bascule pose donc l'attribut
  // sur <html>, pas sur ce conteneur, pour que toute la page (y compris la
  // barre de touches, position fixe) suive le thème choisi.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    return () => { delete document.documentElement.dataset.theme; };
  }, [theme]);

  return (
    <div className="foundation-reference">
      <header className="foundation-reference__header">
        <div>
          <p className="eyebrow">Quadringent — visual foundation</p>
          <h1>Foundation components</h1>
          <p>Development review page. Unavailable in production.</p>
        </div>
        <button type="button" className="action-button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>
          Theme: {theme === 'light' ? 'clair' : 'sombre'}
        </button>
      </header>

      <section aria-labelledby="section-status">
        <h2 id="section-status">StatusWord</h2>
        <div className="foundation-reference__row">
          {STATUS_STATES.map((state) => <StatusWord key={state} state={state} />)}
        </div>
        <div className="foundation-reference__row">
          {STATUS_STATES.map((state) => <StatusWord key={state} state={state} compact />)}
        </div>
      </section>

      <section aria-labelledby="section-metric">
        <h2 id="section-metric">MetricTile</h2>
        <div className="foundation-reference__grid">
          <MetricTile label="Reader lag" value="42" unit="s" provenance="measured" age="il y a 3 min" />
          <MetricTile label="Estimated monthly cost" value="128.40" unit="€" provenance="estimated" age="il y a 1 h" />
          <MetricTile label="Loaded rows" value={null} provenance="absent" />
        </div>
      </section>

      <section aria-labelledby="section-table">
        <h2 id="section-table">BandedTable</h2>
        <BandedTable
          caption="Demonstration source tables"
          columns={columns}
          rows={TABLE_ROWS}
          rowKey={(row) => row.id}
          rowActions={(row) => (
            <ActionButton label="Inspect" onAction={() => pushLog(`Inspect ${row.table}`)} />
          )}
        />
      </section>

      <section aria-labelledby="section-action">
        <h2 id="section-action">ActionButton</h2>
        <div className="foundation-reference__row">
          <ActionButton label="Refresh" onAction={() => pushLog("Refresh")} primary />
          <ActionButton label="Pause" onAction={() => pushLog("Pause confirmed")} requiresConfirmation />
          <ActionButton label="Unavailable" onAction={() => {}} disabled />
        </div>
      </section>

      <section aria-labelledby="section-log">
        <h2 id="section-log">Interaction log</h2>
        <ul className="foundation-reference__log">
          {log.length === 0 ? <li>No interactions yet.</li> : log.map((entry, index) => <li key={index}>{entry}</li>)}
        </ul>
      </section>

      <FunctionKeyBar
        entries={entries}
        onTrigger={(key) => functionKeyBindings.find((binding) => binding.key === key)?.onTrigger()}
      />
    </div>
  );
}
