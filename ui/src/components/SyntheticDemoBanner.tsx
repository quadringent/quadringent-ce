import { SYNTHETIC_DEMO_COPY } from '../domain/operator.ts';
import type { SyntheticDemoProvenance } from '../domain/syntheticDemo.ts';

/** Persistent banner of the synthetic demo build: synthetic values, no
 *  connection and the exact source the demo was built from. */
export function SyntheticDemoBanner({ provenance }: { readonly provenance: SyntheticDemoProvenance }) {
  return (
    <section className="synthetic-demo-banner" role="region" aria-label={SYNTHETIC_DEMO_COPY.title}>
      <p className="synthetic-demo-banner__title"><strong>{SYNTHETIC_DEMO_COPY.title}</strong></p>
      <p>{SYNTHETIC_DEMO_COPY.connection} {SYNTHETIC_DEMO_COPY.values}</p>
      <p className="synthetic-demo-banner__provenance">
        {SYNTHETIC_DEMO_COPY.build} <span className="mono">{provenance.version}</span>
        {' · '}{SYNTHETIC_DEMO_COPY.commit} <span className="mono">{provenance.sourceCommit.slice(0, 7)}</span>
        {' · '}{SYNTHETIC_DEMO_COPY.builtAt} <time className="mono" dateTime={provenance.builtAtUtc}>{provenance.builtAtUtc}</time>
        {' · '}{SYNTHETIC_DEMO_COPY.scope}
      </p>
    </section>
  );
}
