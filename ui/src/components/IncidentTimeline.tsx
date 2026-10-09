import type { Pipeline, StageId } from '../domain/controlPlane.ts';
import { buildIncidentContext, stageLabel } from '../domain/pipelineDetail.ts';
import type { ProofFocus } from '../domain/proofFocus.ts';
import { failClosedFrontier } from './proofStations.ts';

export function IncidentTimeline({ pipeline, current = true, proofScope, focusStage = null }: {
  readonly pipeline: Pipeline;
  readonly current?: boolean;
  readonly proofScope?: ProofFocus['proofScope'];
  readonly focusStage?: StageId | null;
}) {
  const context = buildIncidentContext(pipeline);
  const retained = proofScope?.kind === 'cached';
  const recovering = pipeline.status === 'recovering';
  const activeIncident = current && pipeline.status === 'incident';
  const frontier = failClosedFrontier(focusStage);
  if (context) {
    return (
      <section className="incident-context" aria-labelledby="incident-context-title">
        <div className="section-head">
          <div>
            <p className="section-kicker">{recovering ? "Recovery observed" : activeIncident ? "Current incident" : retained ? "Retained incident" : "Incident observed"}</p>
            <h2 className="section-title" id="incident-context-title">{context.publicLabel}</h2>
          </div>
          {activeIncident ? <span className="incident-context__active">Active</span> : recovering ? <span className="incident-context__recovery">Recovery to confirm</span> : null}
        </div>
        <dl className="incident-context__facts">
          <div><dt>Safe explanation</dt><dd>{context.safeExplanation}</dd></div>
          <div><dt>Affected stage</dt><dd>{context.affectedStage}</dd></div>
          <div><dt>First observation</dt><dd>Not published by the API</dd></div>
          <div><dt>Last observation</dt><dd><time dateTime={context.lastObservedAt}>{context.lastObservedAt}</time></dd></div>
          <div><dt>{activeIncident ? "Recommended action" : "Safe check"}</dt><dd>{activeIncident ? "No automated safe action; verify connectivity and protected data plane logs." : "No current operator action is established; verify connectivity and protected logs before deciding."}</dd></div>
        </dl>
        {recovering ? (
          <p className="incident-context__derived">Recovery observed · completion to confirm. The incident signal remains an observation until recovery is established.</p>
        ) : !activeIncident ? (
          <p className="incident-context__derived">Scope · {proofScope?.label ?? "Observation not current"}. This finding does not describe a current operator situation.</p>
        ) : null}
        <p className="incident-context__history">Past incident history not published by the API.</p>
      </section>
    );
  }

  const emptyTitle = recovering
    ? "Recovery to confirm"
    : current
      ? "No current structured incident"
      : retained
        ? "No structured incident in the retained snapshot"
        : "No structured incident observed";

  return (
    <section className="incident-context incident-context--register" aria-labelledby="incident-context-title">
      <p className="section-kicker">{recovering ? "Recovery observed" : "Incident context"}</p>
      <h2 className="section-title" id="incident-context-title">{emptyTitle}</h2>
      <p className="incident-context__register-fact">
        Evidence boundary · {stageLabel(frontier)}. This finding is not a structured incident.
      </p>
      {pipeline.status === 'incident' ? (
        <p className="incident-context__derived"><strong>derived signal</strong> — the verdict is Incident, without an associated public incident object.</p>
      ) : null}
      {recovering ? (
        <p className="incident-context__derived">Recovery is observed in this snapshot; its completion remains unconfirmed.</p>
      ) : !current ? <p className="incident-context__derived">Scope · {proofScope?.label ?? "Observation not current"}. This observation does not prescribe an operator action.</p> : null}
      <p className="incident-context__history">Past incident history not published by the API.</p>
    </section>
  );
}
