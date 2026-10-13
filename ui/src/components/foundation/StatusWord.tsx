import { statusWordCopy, type StatusWordState } from '../../domain/operator.ts';

export { statusWordCopy, type StatusWordCopy, type StatusWordState } from '../../domain/operator.ts';

// Keep a visible word beside the decorative mark; color alone carries no meaning.

export function StatusWord({
  state,
  compact = false,
}: {
  readonly state: StatusWordState;
  /** Rendu resserré pour une cellule de tableau (BandedTable) ou une liste dense. */
  readonly compact?: boolean;
}) {
  const copy = statusWordCopy(state);
  return (
    <span
      className={`status-word status-word--${state}${compact ? ' status-word--compact' : ''}`}
      role="status"
    >
      <span aria-hidden="true" className="status-word__mark" />
      <span className="status-word__label">{copy.label}</span>
      <span className="sr-only"> — {copy.description}</span>
    </span>
  );
}
