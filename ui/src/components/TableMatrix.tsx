import { useState } from 'react';
import { sequences } from '../domain/format.ts';
import type { BoardTableRow } from '../domain/liveBoard.ts';
import { copiedCopy, identityStatusLabel, journalImagesLabel } from './SourceTables.tsx';

// Cell color follows the measured phase; fill height shows relative volume.
// Missing measurements remain absent in the tooltip and expanded facts.

type CellTone = 'ok' | 'live' | 'warn' | 'down' | 'off';

/* La couleur porte l'état MESURÉ (row.phase), pas le ton écran (muté quand
 * le relevé est retenu). Une table certifiée figée rend une cellule verte
 * que la classe `tmat--held` désature — « photo fanée » de l'état connu,
 * jamais une case vide qui ferait croire à une absence de donnée. */
function cellTone(row: BoardTableRow): CellTone {
  if (row.problem === 'blocked') return 'down';
  if (row.problem === 'journal') return 'warn';
  if (row.problem === 'unknown' || row.phase === null) return 'off';
  switch (row.phase) {
    case 'CERTIFIED': return 'ok';
    case 'LIVE':
    case 'CATCHING_UP':
    case 'RECONCILING':
    case 'HISTORICAL': return 'live';
    case 'PAUSED': return 'warn';
    default: return 'off'; // READY, PREPARED, UNKNOWN — pas encore mesuré
  }
}

/** Glyphe minimal dans la cellule — la couleur parle d'abord, le glyphe
 *  nomme ce que la couleur ne peut pas : blocage, pause, non mesuré. */
function cellGlyph(row: BoardTableRow): string | null {
  if (row.problem === 'blocked') return '!';
  if (row.problem === 'journal') return '~';
  if (row.paused) return '‖';
  if (row.problem === 'unknown' || row.phase === null) return '—';
  return null;
}

function cellTitle(row: BoardTableRow): string {
  const state = row.state.secondary !== null ? `${row.state.primary} · ${row.state.secondary}` : row.state.primary;
  return `${row.name} — ${state} · ${copiedCopy(row)} · images ${journalImagesLabel(row)} · identity ${identityStatusLabel(row)}`;
}

export function TableMatrix({
  rows,
  pipelineId,
  detailHref,
  held = false,
}: {
  readonly rows: readonly BoardTableRow[];
  readonly pipelineId: string;
  /** L0 — lien vers le détail de la source dans le panneau d'expansion. */
  readonly detailHref?: string;
  /** Relevé retenu/figé — la matrice se désature en « photo fanée ». */
  readonly held?: boolean;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const detailId = `${pipelineId}-tables-detail`;
  const openRow = rows.find((row) => row.name === open) ?? null;

  return (
    <div className={`tmat${held ? ' tmat--held' : ''}`}>
      <p className="sr-only">{`Tables in ${pipelineId} — issues first`}</p>
      <ul className="tmat__cells">
        {rows.length === 0 && (
          <li className="tmat__empty">
            No table in the observation — catalog and execution not projected
          </li>
        )}
        {rows.map((row) => {
          const tone = cellTone(row);
          const glyph = cellGlyph(row);
          const expanded = open === row.name;
          return (
            <li key={row.name} className="tmat__item">
              <button
                type="button"
                className={`tcell tcell--${tone}${row.problem !== null ? ' tcell--problem' : ''}`}
                data-table={row.name}
                aria-expanded={expanded}
                aria-controls={detailId}
                aria-label={`${row.name} — ${row.state.primary}`}
                title={cellTitle(row)}
                onClick={() => setOpen(expanded ? null : row.name)}
              >
                <span className="tcell__box" aria-hidden="true">
                  {row.volumeShare !== null && (
                    <i
                      className="tcell__fill"
                      style={{ height: `${Math.max(4, Math.round(row.volumeShare * 100))}%` }}
                    />
                  )}
                  {glyph !== null && <i className="tcell__glyph">{glyph}</i>}
                </span>
                <span className="tcell__name">{row.name}</span>
                {row.volumeShare !== null && (
                  <span className="sr-only">{`Relative volume ${Math.round(row.volumeShare * 100)} % of the largest table`}</span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
      <div className="tmat__detail" id={detailId}>
        {openRow !== null && <TableFacts row={openRow} detailHref={detailHref} />}
      </div>
    </div>
  );
}

/** Faits exacts d'une table — la couche chiffres, à la demande. */
function TableFacts({ row, detailHref }: { readonly row: BoardTableRow; readonly detailHref?: string }) {
  return (
    <div className="tmat__facts">
      <dl className="source-table__facts">
        <div>
          <dt>State</dt>
          <dd>
            {row.state.primary}
            {row.state.secondary !== null ? ` · ${row.state.secondary}` : ''}
          </dd>
        </div>
        <div>
          <dt>Catalog volume</dt>
          <dd>
            {row.totalRows !== null
              ? `${sequences(row.totalRows)} rows${row.cataloguedAt !== null ? ` · observation ${row.cataloguedAt}` : ''}`
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
          <dt>Identity</dt>
          <dd>
            {row.identityStatus === 'proven' || row.identityStatus === 'keyed'
              ? "Proven — business key"
              : row.identityStatus === 'rrn'
                ? "Proven — physical position (RRN)"
                : row.identityStatus === 'blocked'
                  ? `Unproven${row.blockedReasons.length > 0 ? ` — ${row.blockedReasons.join(', ')}` : ''}`
                  : "Unknown — catalog not read"}
          </dd>
        </div>
        <div>
          <dt>Per-table measurements</dt>
          <dd>Not measured per table — throughput and destination aggregated over the whole flow</dd>
        </div>
      </dl>
      {detailHref !== undefined && (
        <a className="source-table__more" href={detailHref}>
          Open source details
        </a>
      )}
    </div>
  );
}
