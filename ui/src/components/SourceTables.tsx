import { Fragment, useState } from 'react';
import type { FluxIdentity } from '../domain/controlPlane.ts';
import { sequences } from '../domain/format.ts';
import type { BoardTableRow } from '../domain/liveBoard.ts';
import { fluxObjectCoverage } from '../domain/sourceDetail.ts';
import '../styles/source-tables.css';

/**
 * Grille des tables — l'objet signature, partagée entre le plateau (L0) et
 * l'onglet Tables du détail source (L2).
 *
 * Une ligne par table : nom métier · état fusionné (phase + verdict) · lignes
 * copiées/total avec barre de volume relatif · images journal. En mode détail
 * (`detailed`), une colonne Identité et une ligne de couverture par le flux
 * rejoignent les faits d'expansion. Tout ce qui n'est pas mesuré par table
 * est dit « non instrumenté », jamais zéro.
 */

export function copiedCopy(row: BoardTableRow): string {
  if (row.copiedRows === null && row.totalRows === null) return "Unknown — not measured";
  const copied = row.copiedRows === null ? 'inconnu' : sequences(row.copiedRows);
  const total = row.totalRows === null ? 'inconnu' : sequences(row.totalRows);
  return `${copied} / ${total}`;
}

export function journalImagesLabel(row: BoardTableRow): string {
  if (row.journalImages === null) return "Unknown — catalog not read";
  return row.journalImages;
}

export function identityStatusLabel(row: BoardTableRow): string {
  switch (row.identityStatus) {
    case 'keyed': return "Business key";
    case 'rrn': return 'RRN';
    case 'blocked': return "Unproven";
    default: return "Unknown — catalog not read";
  }
}

export function SourceTables({
  rows,
  pipelineId,
  footer,
  detailHref,
  detailed = false,
  flux,
  footnote = null,
}: {
  readonly rows: readonly BoardTableRow[];
  readonly pipelineId: string;
  readonly footer: string;
  /** L0 uniquement — lien vers le détail de la source. */
  readonly detailHref?: string;
  /** L2 — ajoute la colonne Identité et la couverture par les objets du flux. */
  readonly detailed?: boolean;
  readonly flux?: FluxIdentity | null;
  /** Note de distinction sous le footer (ex. catalogue déclaré vs copiées
   *  mesurées) — jamais un avertissement inventé. */
  readonly footnote?: string | null;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const columnCount = detailed ? 5 : 4;

  return (
    <table className="source-tables">
      <caption className="sr-only">{`Tables in ${pipelineId} — issues first`}</caption>
      <thead>
        <tr>
          <th scope="col">Table</th>
          <th scope="col">State</th>
          <th scope="col">Copied / total · relative volume</th>
          <th scope="col">Journal images</th>
          {detailed && <th scope="col">Identity</th>}
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={columnCount} className="source-tables__empty">
              No table in the observation — catalog and execution not projected
            </td>
          </tr>
        )}
        {rows.map((row) => {
          const expanded = open === row.name;
          const detailId = `${pipelineId}-table-${row.name}`;
          return (
            <Fragment key={row.name}>
              <tr
                className={`source-table${row.problem !== null ? ' source-table--problem' : ''}`}
                data-table={row.name}
              >
                <th scope="row">
                  <button
                    type="button"
                    className="source-table__toggle"
                    aria-expanded={expanded}
                    aria-controls={detailId}
                    onClick={() => setOpen(expanded ? null : row.name)}
                  >
                    <span className="source-table__chevron" aria-hidden="true">
                      {expanded ? '▾' : '▸'}
                    </span>
                    <span className="mono">{row.name}</span>
                  </button>
                </th>
                <td>
                  <span className={`table-verdict table-verdict--${row.state.tone}`}>
                    {row.state.primary}
                  </span>
                  {row.state.secondary !== null && (
                    <span className="source-table__phase-sub">{row.state.secondary}</span>
                  )}
                </td>
                <td className="source-table__copied">
                  <span className="source-table__count" data-numeric>
                    {copiedCopy(row)}
                  </span>
                  {row.volumeShare !== null && (
                    <span
                      className="source-progress"
                      role="img"
                      aria-label={`Relative volume ${Math.round(row.volumeShare * 100)} % of the largest table`}
                    >
                      <i style={{ width: `${Math.max(1, Math.round(row.volumeShare * 100))}%` }} />
                    </span>
                  )}
                </td>
                <td className={row.problem === 'journal' ? 'source-table__flag' : undefined}>
                  {journalImagesLabel(row)}
                </td>
                {detailed && (
                  <td className={row.identityStatus === 'blocked' ? 'source-table__flag' : undefined}>
                    {identityStatusLabel(row)}
                  </td>
                )}
              </tr>
              {expanded && (
                <tr className="source-table__detail" id={detailId}>
                  <td colSpan={columnCount}>
                    <dl className="source-table__facts">
                      <div>
                        <dt>Catalog volume</dt>
                        <dd>
                          {row.totalRows !== null
                            ? `${sequences(row.totalRows)} rows${
                                row.cataloguedAt !== null ? ` · observation ${row.cataloguedAt}` : ''
                              }`
                            : "Unknown — catalog not read"}
                        </dd>
                      </div>
                      <div>
                        <dt>Measured copy</dt>
                        <dd>
                          {row.progress !== null
                            ? `${Math.round(row.progress * 100)} % of the table copied`
                            : row.copiedRows !== null
                              ? `${sequences(row.copiedRows)} rows copied — total unknown`
                              : "Unknown — copy not measured"}
                        </dd>
                      </div>
                      <div>
                        <dt>Journal images</dt>
                        <dd>
                          {row.journalImages === '*BOTH'
                            ? "Before + after (*BOTH)"
                            : row.journalImages === '*AFTER'
                              ? "After each change (*AFTER)"
                              : row.journalImages === '*BEFORE'
                                ? "Before each change (*BEFORE) — unusable"
                                : "Unknown — catalog not read"}
                        </dd>
                      </div>
                      <div>
                        <dt>Historical copy</dt>
                        <dd>
                          {row.cataloguedAt === null
                            ? "Unknown — plan not read"
                            : row.admitted
                              ? "Admitted to the manifest"
                              : "Not admitted to the manifest"}
                        </dd>
                      </div>
                      <div>
                        <dt>Measured phase</dt>
                        <dd>{`${row.phaseLabel} · ${copiedCopy(row)}`}</dd>
                      </div>
                      <div>
                        <dt>Identity</dt>
                        <dd>
                          {row.identityStatus === 'proven' || row.identityStatus === 'keyed'
                            ? "Proven — business key"
                            : row.identityStatus === 'rrn'
                              ? "Proven — physical position (RRN)"
                              : row.identityStatus === 'blocked'
                                ? `Unproven${
                                    row.blockedReasons.length > 0
                                      ? ` — ${row.blockedReasons.join(', ')}`
                                      : ''
                                  }`
                                : "Unknown — catalog not read"}
                        </dd>
                      </div>
                      {detailed && (
                        <div>
                          <dt>Flow coverage</dt>
                          <dd>{fluxObjectCoverage(flux, row.name)}</dd>
                        </div>
                      )}
                      <div>
                        <dt>Per-table measurements</dt>
                        <dd>
                          Not measured per table — throughput and destination aggregated over the whole flow
                        </dd>
                      </div>
                    </dl>
                    {detailHref !== undefined && (
                      <a className="source-table__more" href={detailHref}>
                        Open source details
                      </a>
                    )}
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
      <tfoot>
        <tr>
          <td colSpan={columnCount} className="source-tables__footer">
            {footer}
            {footnote !== null && (
              <span className="source-tables__note">{footnote}</span>
            )}
          </td>
        </tr>
      </tfoot>
    </table>
  );
}
