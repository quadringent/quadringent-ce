import type { ObservationQuality as Quality } from '../domain/controlPlane.ts';

const coverage = {
  complete: "Complete coverage",
  partial: "Partial coverage",
  gap: "Telemetry gap",
  none: "No coverage",
} as const;

const freshness = {
  late: "Late observation",
  stale: "Expired observation",
  clock_untrusted: "Clock unreliable",
} as const;

const evidence = {
  live: "Actual system",
  historical: "Historical evidence",
  simulation: 'Simulation',
} as const;

export function coverageLabel(quality: Quality): string {
  return coverage[quality.coverage];
}

export function evidenceLabel(kind: Quality['evidenceKind'] | Quality): string {
  return evidence[typeof kind === 'string' ? kind : kind.evidenceKind];
}

export function freshnessLabel(quality: Quality): string {
  if (quality.freshness !== 'fresh') return freshness[quality.freshness];
  return freshObservationLabel(quality);
}

export function registerFreshnessLabel(quality: Quality): string {
  switch (quality.freshness) {
    case 'stale': return "Expired observation";
    case 'late': return "Late observation";
    case 'clock_untrusted': return "Clock unreliable";
    case 'fresh':
      if (quality.evidenceKind === 'historical') return "Fresh at snapshot time";
      if (quality.evidenceKind === 'simulation') return "Fresh in the simulation";
      return "Fresh observation";
  }
}

export function proofScopeNamesFreshness(label: string): boolean {
  return /stale|late|unreliable clock|périmé|tardive|horloge non fiable/i.test(label);
}

export function ObservationQuality({ quality }: { readonly quality: Quality }) {
  return (
    <dl className="observation-quality" aria-label="Observation quality">
      <div className="observation-quality__item">
        <dt>Coverage</dt>
        <dd>{coverageLabel(quality)}</dd>
      </div>
      <div className="observation-quality__item">
        <dt>Freshness</dt>
        <dd>{freshnessLabel(quality)}</dd>
      </div>
      <div className="observation-quality__item">
        <dt>Provenance</dt>
        <dd>{evidenceLabel(quality)}</dd>
      </div>
    </dl>
  );
}

function freshObservationLabel(quality: Quality): string {
  if (quality.evidenceKind === 'historical') return "Fresh at historical snapshot time · current expiry not established";
  if (quality.evidenceKind === 'simulation') return "Fresh in the simulated scenario · live freshness not established";
  return "Fresh observation";
}
