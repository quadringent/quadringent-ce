import { useEffect, useId, useRef } from 'react';
import type { Pipeline, Stage } from '../domain/controlPlane.ts';
import { buildStageProof, stageLabel } from '../domain/pipelineDetail.ts';
import type { ProofFocus } from '../domain/proofFocus.ts';

const focusableSelector = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function StageDrawer({ pipeline, stage, current, proofScope, onClose }: {
  readonly pipeline: Pipeline;
  readonly stage: Stage;
  readonly current: boolean;
  readonly proofScope: ProofFocus['proofScope'];
  readonly onClose: () => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const proof = buildStageProof(stage, pipeline);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? [])];
      if (!focusable.length) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (event.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [onClose]);

  return (
    <div className="stage-dialog-layer">
      <button className="stage-dialog__backdrop" type="button" tabIndex={-1} aria-label="Close stage evidence" onClick={onClose} />
      <aside ref={dialogRef} className="stage-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1}>
        <div className="stage-drawer__head">
          <div>
            <p className="section-kicker">Stage evidence</p>
            <h2 className="stage-drawer__title" id={titleId}>{stageLabel(stage.id)}</h2>
          </div>
          <button ref={closeRef} className="stage-drawer__close" type="button" onClick={onClose}>Close</button>
        </div>
        <p className={`stage-drawer__status stage-drawer__status--${stage.status}`}>
          {current ? proof.statusLabel : proofScope.kind === 'cached' ? "Retained observation" : "Observation not current"}
        </p>
        <dl className="stage-drawer__facts">
          <div><dt>{current ? "Finding" : "Raw observation"}</dt><dd>{current ? stage.headline : <q>{stage.headline}</q>}</dd></div>
          <div><dt>{current ? "Safe detail" : "Raw detail"}</dt><dd>{current ? stage.detail : <q>{stage.detail}</q>}</dd></div>
          <div><dt>Last observation</dt><dd>{stage.observedAt ? <time dateTime={stage.observedAt}>{formatTimestamp(stage.observedAt)}</time> : "Not observed"}</dd></div>
          <div><dt>Provenance</dt><dd>{proof.provenance}{current ? null : ` · Scope: ${proofScope.label} · not current`}</dd></div>
        </dl>
        <p className="stage-drawer__limit" id={descriptionId}>
          {current
            ? "The v1 snapshot exposes an aggregate finding. Missing metrics and dependencies remain unmeasured."
            : `Scope not current · ${proofScope.label}. This raw observation describes the received snapshot and does not qualify the current state.`}
        </p>
      </aside>
    </div>
  );
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-US", { hourCycle: 'h23', dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' }).format(new Date(value));
}
