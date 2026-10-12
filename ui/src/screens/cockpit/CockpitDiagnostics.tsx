/**
 * Cockpit — Diagnostics. Read-only evidence by layer: which problem is
 * established, about what, since when, which observation proves it, what to
 * do next and what the evidence does not cover. Reading a diagnostic changes
 * nothing, and this screen proposes no repair action.
 *
 * `CockpitDiagnosticsView` is pure (state in props) so it renders without a
 * network; `CockpitDiagnosticsScreen` connects it to the data hooks.
 */
import { useEffect, useState } from 'react';
import { FunctionKeyBar } from '../../components/foundation/FunctionKeyBar.tsx';
import { useFunctionKeys, type FunctionKeyBinding } from '../../components/foundation/useFunctionKeys.ts';
import { useDiagnostics, useDiagnosticsExport, type DiagnosticsExportState, type DiagnosticsState } from '../../data/useDiagnostics.ts';
import type { DiagnosticExport } from '../../data/diagnosticsContract.ts';
import {
  buildDiagnosticsView,
  evidenceRows,
  type DiagnosticResultView,
  type DiagnosticsFailure,
} from '../../domain/diagnostics.ts';
import { browserDownloadEnv, downloadExport, exportResultCount, type ExportIntegrity } from '../../domain/diagnosticsExport.ts';
import { DIAGNOSTICS_COPY } from '../../domain/operator.ts';
import { href } from '../../router.ts';
import { navigateBack } from './cockpitFunctionKeys.ts';

export function CockpitDiagnosticsScreen({ pipelineId }: { readonly pipelineId: string | null }) {
  const { state, reload } = useDiagnostics(pipelineId);
  const { state: exportState, prepare, discard } = useDiagnosticsExport();
  const [downloaded, setDownloaded] = useState(false);

  useEffect(() => { setDownloaded(false); }, [exportState]);
  useEffect(() => {
    document.title = `Quadringent · ${DIAGNOSTICS_COPY.title}`;
  }, []);

  const download = () => {
    if (exportState.status !== 'ready' || exportState.integrity.problems.length > 0) return;
    downloadExport(exportState.exported, browserDownloadEnv());
    setDownloaded(true);
  };

  return (
    <CockpitDiagnosticsView
      pipelineId={pipelineId}
      state={state}
      onRefresh={reload}
      exportState={exportState}
      onPrepareExport={prepare}
      onDiscardExport={discard}
      onDownloadExport={download}
      downloaded={downloaded}
    />
  );
}

export interface CockpitDiagnosticsViewProps {
  readonly pipelineId: string | null;
  readonly state: DiagnosticsState;
  readonly onRefresh: () => void;
  readonly exportState: DiagnosticsExportState;
  readonly onPrepareExport: () => void;
  readonly onDiscardExport: () => void;
  readonly onDownloadExport: () => void;
  readonly downloaded: boolean;
}

export function CockpitDiagnosticsView({
  pipelineId,
  state,
  onRefresh,
  exportState,
  onPrepareExport,
  onDiscardExport,
  onDownloadExport,
  downloaded,
}: CockpitDiagnosticsViewProps) {
  const bindings: readonly FunctionKeyBinding[] = [
    { key: 'F3', onTrigger: navigateBack },
    { key: 'F5', onTrigger: onRefresh },
  ];
  const entries = useFunctionKeys(bindings);

  return (
    <div className="board cockpit-diagnostics">
      <header className="board__head">
        <div>
          <p className="board__kicker">
            <a href={href({ name: 'cockpit' })}>Cockpit</a>
            {pipelineId ? <> · <a href={href({ name: 'cockpit-table', id: pipelineId, tab: 'proofs' })}>{pipelineId}</a></> : null}
          </p>
          <h1 className="board__title">{DIAGNOSTICS_COPY.title}</h1>
          <p className="board__summary">{DIAGNOSTICS_COPY.summary}</p>
        </div>
        <div className="cockpit-home__header-actions">
          <button type="button" className="board__refresh" onClick={onRefresh}>{DIAGNOSTICS_COPY.refresh}</button>
        </div>
      </header>

      {pipelineId ? (
        <p className="cockpit-diagnostics__filter">
          {DIAGNOSTICS_COPY.filteredBy} <span className="mono">{pipelineId}</span>
          {' · '}
          <a href={href({ name: 'cockpit-diagnostics' })}>{DIAGNOSTICS_COPY.clearFilter}</a>
        </p>
      ) : null}

      {state.status === 'loading' ? (
        <section className="board__empty" role="status"><h2>{DIAGNOSTICS_COPY.loading}</h2></section>
      ) : state.status === 'failed' ? (
        <FailureNotice failure={state.failure} />
      ) : (
        <ReportView state={state} />
      )}

      {state.status === 'ready' ? (
        <ExportPanel
          pipelineId={pipelineId}
          state={exportState}
          onPrepare={onPrepareExport}
          onDiscard={onDiscardExport}
          onDownload={onDownloadExport}
          downloaded={downloaded}
        />
      ) : null}

      <FunctionKeyBar entries={entries} onTrigger={(key) => bindings.find((binding) => binding.key === key)?.onTrigger()} />
    </div>
  );
}

function FailureNotice({ failure }: { readonly failure: DiagnosticsFailure }) {
  return (
    <section className="board__empty cockpit-diagnostics__failure" role="alert">
      <h2>{failure.title}</h2>
      <p>{failure.detail}</p>
      {failure.serviceMessage ? <p className="mono">{failure.serviceMessage}</p> : null}
      {failure.retryAfterSeconds !== null ? <p>{DIAGNOSTICS_COPY.retryAfter(failure.retryAfterSeconds)}</p> : null}
    </section>
  );
}

function ReportView({ state }: { readonly state: Extract<DiagnosticsState, { status: 'ready' }> }) {
  const { report } = state;
  const view = buildDiagnosticsView(report);
  return (
    <>
      <section className="cockpit-diagnostics__overall" aria-label="Overview">
        <p className={`cockpit-diagnostics__overall-text cockpit-diagnostics__overall-text--${view.overall.status}`}>{view.overall.text}</p>
        <p className="cockpit-diagnostics__note">{DIAGNOSTICS_COPY.noticeNotAHealthVerdict}</p>
        {report.synthetic ? <p className="cockpit-diagnostics__synthetic" role="note">{DIAGNOSTICS_COPY.syntheticReport}</p> : null}
        <ul className="cockpit-diagnostics__counts">
          <li>{DIAGNOSTICS_COPY.counts.passed} <span className="mono">{view.counts.passed}</span></li>
          <li>{DIAGNOSTICS_COPY.counts.failed} <span className="mono">{view.counts.failed}</span></li>
          <li>{DIAGNOSTICS_COPY.counts.unknown} <span className="mono">{view.counts.unknown}</span></li>
          <li>{DIAGNOSTICS_COPY.counts.skipped} <span className="mono">{view.counts.skipped}</span></li>
        </ul>
        {view.countsConsistent ? null : <p className="cockpit-diagnostics__note">{DIAGNOSTICS_COPY.countsMismatch}</p>}
        <dl className="cockpit-diagnostics__facts">
          <div><dt>{DIAGNOSTICS_COPY.scopeLabel}</dt><dd>{DIAGNOSTICS_COPY.scope[report.scope]}</dd></div>
          <div><dt>{DIAGNOSTICS_COPY.generatedAt}</dt><dd><time dateTime={report.generatedAt}>{utcText(report.generatedAt)}</time></dd></div>
          <div><dt>{DIAGNOSTICS_COPY.software}</dt><dd className="mono">{[report.software.product, report.software.version].filter(Boolean).join(' ') || '—'}{report.software.distribution ? ` · ${report.software.distribution}` : ''}</dd></div>
        </dl>
      </section>

      {view.empty ? (
        <section className="board__empty"><h2>{DIAGNOSTICS_COPY.noResults}</h2></section>
      ) : (
        <>
          <section className="cockpit-diagnostics__section" aria-labelledby="diag-problems">
            <h2 id="diag-problems">{DIAGNOSTICS_COPY.problemsTitle}</h2>
            {view.problems.length === 0
              ? <p className="cockpit-diagnostics__none">{DIAGNOSTICS_COPY.problemsNone}</p>
              : <ul className="cockpit-diagnostics__list">{view.problems.map((item) => <li key={cardKey(item)}><ResultCard item={item} /></li>)}</ul>}
          </section>

          <section className="cockpit-diagnostics__section" aria-labelledby="diag-gaps">
            <h2 id="diag-gaps">{DIAGNOSTICS_COPY.gapsTitle}</h2>
            <p className="cockpit-diagnostics__note">{DIAGNOSTICS_COPY.gapsHint}</p>
            {view.gaps.length === 0
              ? <p className="cockpit-diagnostics__none">{DIAGNOSTICS_COPY.gapsNone}</p>
              : <ul className="cockpit-diagnostics__list">{view.gaps.map((item) => <li key={cardKey(item)}><ResultCard item={item} /></li>)}</ul>}
          </section>

          <section className="cockpit-diagnostics__section" aria-labelledby="diag-layers">
            <h2 id="diag-layers">{DIAGNOSTICS_COPY.layersTitle}</h2>
            {view.groups.map((group) => (
              <div className="cockpit-diagnostics__layer" key={group.layer}>
                <h3>{group.label}</h3>
                <ul className="cockpit-diagnostics__list">
                  {group.views.map((item) => <li key={cardKey(item)}><ResultCard item={item} compact /></li>)}
                </ul>
              </div>
            ))}
          </section>
        </>
      )}
    </>
  );
}

function cardKey(item: DiagnosticResultView): string {
  return `${item.result.code}|${item.result.subject?.kind ?? ''}|${item.result.subject?.ref ?? ''}`;
}

function utcText(value: string): string {
  return value.replace('T', ' ').replace(/\.\d+/, '').replace(/(Z|[+-]00:00)$/, ' UTC');
}

/** One check. `compact` folds the proof behind a native disclosure, keyboard
 *  reachable, so the by-layer list stays scannable. */
function ResultCard({ item, compact = false }: { readonly item: DiagnosticResultView; readonly compact?: boolean }) {
  const { result } = item;
  const rows = evidenceRows(result);
  const body = (
    <dl className="diagnostic-result__facts">
      {item.subjectText ? (
        <div>
          <dt>{DIAGNOSTICS_COPY.subject}</dt>
          <dd>
            {item.subjectText}
            {item.subjectRoute ? (
              <>
                {' · '}
                <a href={href(item.subjectRoute)}>
                  {item.subjectRoute.name === 'cockpit-table' ? DIAGNOSTICS_COPY.openTable : DIAGNOSTICS_COPY.openConnection}
                </a>
              </>
            ) : null}
          </dd>
        </div>
      ) : null}
      {result.since && item.sinceText ? (
        <div><dt>{DIAGNOSTICS_COPY.since}</dt><dd><time dateTime={result.since}>{item.sinceText}</time></dd></div>
      ) : null}
      <div>
        <dt>{item.observedLabel}</dt>
        <dd>{result.observedAt && item.observedText ? <time dateTime={result.observedAt}>{item.observedText}</time> : DIAGNOSTICS_COPY.notObserved}</dd>
      </div>
      <div><dt>{DIAGNOSTICS_COPY.provenance}</dt><dd className="mono">{result.provenance}</dd></div>
      <div>
        <dt>{DIAGNOSTICS_COPY.evidence}</dt>
        <dd>
          {rows.length === 0 ? DIAGNOSTICS_COPY.evidenceNone : (
            <ul className="diagnostic-result__evidence">
              {rows.map((row) => <li key={row.key}><span>{row.label}</span> <span className="mono">{row.value}</span></li>)}
            </ul>
          )}
        </dd>
      </div>
      <div><dt>{DIAGNOSTICS_COPY.nextAction}</dt><dd>{result.nextAction ?? DIAGNOSTICS_COPY.nextActionNone}</dd></div>
      <div><dt>{DIAGNOSTICS_COPY.limits}</dt><dd>{result.limits ?? DIAGNOSTICS_COPY.limitsNone}</dd></div>
    </dl>
  );

  return (
    <article className={`diagnostic-result diagnostic-result--${item.tone}`}>
      <header className="diagnostic-result__head">
        <span className={`diagnostic-status diagnostic-status--${item.tone}`}>
          {item.statusWord}
          {item.qualifier ? <span className="diagnostic-status__qualifier"> · {item.qualifier}</span> : null}
        </span>
        <span className="diagnostic-result__code mono">{result.code}</span>
        <span className="diagnostic-result__required">{result.required ? DIAGNOSTICS_COPY.required : DIAGNOSTICS_COPY.optional}</span>
      </header>
      <p className="diagnostic-result__summary">{result.summary}</p>
      {compact && item.recordRead ? <p className="diagnostic-result__limits">{result.limits ?? DIAGNOSTICS_COPY.limitsNone}</p> : null}
      {compact ? (
        <details className="diagnostic-result__details">
          <summary>{DIAGNOSTICS_COPY.detailsSummary}</summary>
          {body}
        </details>
      ) : body}
    </article>
  );
}

function ExportPanel({
  pipelineId,
  state,
  onPrepare,
  onDiscard,
  onDownload,
  downloaded,
}: {
  readonly pipelineId: string | null;
  readonly state: DiagnosticsExportState;
  readonly onPrepare: () => void;
  readonly onDiscard: () => void;
  readonly onDownload: () => void;
  readonly downloaded: boolean;
}) {
  const copy = DIAGNOSTICS_COPY.export;
  return (
    <section className="cockpit-diagnostics__section cockpit-diagnostics__export" aria-labelledby="diag-export">
      <h2 id="diag-export">{copy.title}</h2>
      {state.status === 'idle' || state.status === 'failed' ? (
        <>
          <p className="cockpit-diagnostics__note">{copy.intro}</p>
          {state.status === 'failed' ? <FailureNotice failure={state.failure} /> : null}
          <button type="button" className="cockpit-diagnostics__action" onClick={onPrepare}>{copy.prepare}</button>
        </>
      ) : state.status === 'preparing' ? (
        <p role="status">{copy.preparing}</p>
      ) : (
        <ExportPreview
          pipelineId={pipelineId}
          exported={state.exported}
          integrity={state.integrity}
          onDownload={onDownload}
          onDiscard={onDiscard}
          downloaded={downloaded}
        />
      )}
    </section>
  );
}

function ExportPreview({
  pipelineId,
  exported,
  integrity,
  onDownload,
  onDiscard,
  downloaded,
}: {
  readonly pipelineId: string | null;
  readonly exported: DiagnosticExport;
  readonly integrity: ExportIntegrity;
  readonly onDownload: () => void;
  readonly onDiscard: () => void;
  readonly downloaded: boolean;
}) {
  const copy = DIAGNOSTICS_COPY.export;
  const blocked = integrity.problems.length > 0;
  const pseudonyms = Object.entries(exported.preview.pseudonymized);
  return (
    <div className="cockpit-diagnostics__preview">
      <h3>{copy.previewTitle}</h3>
      <p className="cockpit-diagnostics__note">{copy.previewIntro}</p>
      <p className="cockpit-diagnostics__coverage">
        {copy.coverage(exportResultCount(exported))}
        {pipelineId ? <> {copy.filteredNotice(pipelineId)}</> : null}
      </p>
      {blocked ? (
        <div role="alert" className="cockpit-diagnostics__integrity">
          <p><strong>{copy.integrityTitle}</strong></p>
          <ul>{integrity.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul>
        </div>
      ) : null}
      {!blocked && !integrity.verified ? <p className="cockpit-diagnostics__note">{copy.integrity.unverifiable}</p> : null}

      <table className="cockpit-diagnostics__files">
        <caption>{copy.files}</caption>
        <thead>
          <tr><th scope="col">{copy.fileName}</th><th scope="col" className="banded-table__col--numeric">{copy.bytes}</th><th scope="col">{copy.sha256}</th></tr>
        </thead>
        <tbody>
          {exported.preview.files.map((file) => (
            <tr key={file.name}>
              <td className="mono">{file.name}</td>
              <td className="mono banded-table__col--numeric">{file.bytes}</td>
              <td className="mono cockpit-diagnostics__digest">{file.sha256}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>{copy.omitted}</h3>
      {exported.preview.omitted.length === 0 ? <p>{copy.omittedNone}</p> : (
        <ul>{exported.preview.omitted.map((item, index) => <li key={`${index}|${item.field}`}><span className="mono">{item.field}</span> — {item.reason}</li>)}</ul>
      )}

      <h3>{copy.pseudonymized}</h3>
      {pseudonyms.length === 0 ? <p>{copy.pseudonymizedNone}</p> : (
        <dl className="cockpit-diagnostics__facts">
          {pseudonyms.map(([key, count]) => (
            <div key={key}><dt>{copy.pseudonymizedLabels[key] ?? key}</dt><dd className="mono">{count}</dd></div>
          ))}
        </dl>
      )}

      <h3>{copy.limits}</h3>
      <ul>{exported.preview.limits.map((limit) => <li key={limit}>{limit}</li>)}</ul>

      <div className="cockpit-diagnostics__actions">
        <button type="button" className="cockpit-diagnostics__cancel" onClick={onDiscard}>{copy.discard}</button>
        <button type="button" className="cockpit-diagnostics__action cockpit-diagnostics__action--primary" disabled={blocked} onClick={onDownload}>{copy.download}</button>
      </div>
      {downloaded ? <p role="status" className="cockpit-diagnostics__note">{copy.downloaded}</p> : null}
    </div>
  );
}
