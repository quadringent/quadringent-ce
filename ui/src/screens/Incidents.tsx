/**
 * Journal — ce qui s'est passé sur vos liaisons.
 *
 * L'écran répond à « qu'est-ce qui a changé depuis la dernière fois ». Il
 * commence par ce qui demande une attention, puis déroule l'historique en
 * phrases datées. La façon dont le produit sait (relevé mesuré ou transition
 * déduite) ne figure plus sur chaque ligne : elle n'aide aucune décision.
 */

import { useEffect, useState } from 'react';
import type { ControlPlaneState } from '../data/useControlPlane.ts';
import type { Overview, Pipeline } from '../domain/controlPlane.ts';
import { snapshotActivityEvents } from '../domain/activityJournal.ts';
import { activeAlerts, historyFrom, type HistoryLine } from '../domain/history.ts';
import { liaisonView } from '../domain/operator.ts';
import { href } from '../router.ts';
import '../styles/liaison.css';
import '../styles/history.css';

const dayFormat = new Intl.DateTimeFormat("en-US", { hourCycle: 'h23',
  weekday: 'long',
  day: 'numeric',
  month: 'long',
});

const timeFormat = new Intl.DateTimeFormat("en-US", { hourCycle: 'h23',
  hour: '2-digit',
  minute: '2-digit',
});

export function Incidents({
  state,
  pipelines,
  onRefresh,
}: {
  readonly state: ControlPlaneState;
  readonly overview: Overview | null;
  readonly pipelines: readonly Pipeline[];
  readonly onRefresh: () => void;
}) {
  const now = useNow();
  const ready = state.status !== 'loading' && state.status !== 'failed';
  const attention = ready
    ? pipelines.map((pipeline) => liaisonView(pipeline, now)).filter((view) => view.health !== 'ok')
    : [];

  // Les alertes de supervision rejoignent le journal : elles racontent, elles
  // aussi, quelque chose qui s'est passé sur la liaison.
  const lines = ready
    ? historyFrom(pipelines.flatMap((pipeline) => snapshotActivityEvents(pipeline, false)))
    : [];
  const alerts = ready ? pipelines.flatMap(activeAlerts) : [];
  const timeline = [...alerts, ...lines].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));

  return (
    <div className="board">
      <header className="board__head">
        <div>
          <h1 className="board__title">Journal</h1>
          <p className="board__summary">What happened on your connections.</p>
        </div>
        <button type="button" className="board__refresh" onClick={onRefresh}>
          Refresh
        </button>
      </header>

      {attention.length > 0 && (
        <section className="history-attention" aria-label="What needs your attention">
          {attention.map((view) => (
            <a className="history-attention__item" key={view.id} href={href({ name: 'pipeline', id: view.id })}>
              <span className={`liaison__dot liaison--${view.health}`} aria-hidden="true" />
              <span className="history-attention__name">{view.name}</span>
              <span className="history-attention__state">{view.stateLabel}</span>
              <span className="history-attention__headline">{view.headline}</span>
            </a>
          ))}
        </section>
      )}

      {!ready ? (
        <section className="board__empty">
          <h2>{state.status === 'failed' ? "Logs not read" : "Reading"}</h2>
          <p>
            {state.status === 'failed'
              ? "The service did not respond. No event is displayed."
              : "Quadringent is querying the service."}
          </p>
        </section>
      ) : timeline.length === 0 ? (
        <section className="board__empty">
          <h2>Nothing to report</h2>
          <p>No events have been recorded on your connections.</p>
        </section>
      ) : (
        <Timeline lines={timeline} />
      )}
    </div>
  );
}

/** L'historique groupé par jour : la date se lit une fois, pas à chaque ligne. */
function Timeline({ lines }: { readonly lines: readonly HistoryLine[] }) {
  const days = new Map<string, HistoryLine[]>();
  for (const line of lines) {
    const day = dayFormat.format(new Date(line.at));
    const bucket = days.get(day);
    if (bucket) bucket.push(line);
    else days.set(day, [line]);
  }

  return (
    <div className="history">
      {[...days.entries()].map(([day, dayLines]) => (
        <section className="history__day" key={day}>
          <h2 className="history__date">{day}</h2>
          <ol className="history__list">
            {dayLines.map((line) => (
              <li className={`history__line history__line--${line.tone}`} key={`${line.at}-${line.label}`}>
                <time className="history__time" dateTime={line.at}>
                  {timeFormat.format(new Date(line.at))}
                </time>
                <div className="history__body">
                  <p className="history__label">{line.label}</p>
                  {line.detail !== null && <p className="history__detail">{line.detail}</p>}
                </div>
              </li>
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}

function useNow(tickMs = 1000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), tickMs);
    return () => clearInterval(id);
  }, [tickMs]);
  return now;
}
