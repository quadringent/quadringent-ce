import type { Pipeline } from '../domain/controlPlane.ts';
import { href } from '../router.ts';

export interface AttentionItem {
  readonly pipeline: Pipeline;
  readonly cause: string;
}

export function AttentionPanel({ items, emptyConfirmed = false }: {
  readonly items: readonly AttentionItem[];
  readonly emptyConfirmed?: boolean;
}) {
  const groups = groupByCause(items);

  if (!groups.length) {
    return <p className="attention-note" role="note" aria-label="Attention queue">
      {emptyConfirmed
        ? "No other pipeline to inspect in this snapshot."
        : "Attention queue unconfirmed · the available coverage is inconclusive."}
    </p>;
  }

  return (
    <section className="attention-panel" aria-labelledby="attention-title">
      <header>
        <p className="section-kicker">Inspect next</p>
        <h2 id="attention-title">Attention queue</h2>
        <p>After the priority pipeline, grouped by cause.</p>
      </header>
      <div className="attention-panel__groups">
        {groups.map((group) => (
          <section className="attention-group" key={group.cause} aria-label={group.cause}>
            <h3>{group.cause}</h3>
            <ul>
              {group.pipelines.map((pipeline) => (
                <li key={pipeline.id}>
                  <a href={href({ name: 'pipeline', id: pipeline.id })}>{pipeline.id}</a>
                  <span>{pipeline.environment.toLocaleUpperCase("en-US")}</span>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </section>
  );
}

function groupByCause(items: readonly AttentionItem[]): ReadonlyArray<{
  readonly cause: string;
  readonly pipelines: readonly Pipeline[];
}> {
  const groups = new Map<string, Pipeline[]>();
  for (const { pipeline, cause } of items) {
    const group = groups.get(cause) ?? [];
    group.push(pipeline);
    groups.set(cause, group);
  }
  return [...groups.entries()].map(([cause, groupedPipelines]) => ({ cause, pipelines: groupedPipelines }));
}
