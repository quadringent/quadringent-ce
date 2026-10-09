import type { Overview, Scope } from './controlPlane.ts';

export type SourceAvailability = 'available' | 'partial' | 'unavailable' | 'unestablished';

export interface SourceAvailabilityCopy {
  readonly label: string;
  readonly detail: string;
}

export function scopeLabel(scope: Scope | null | undefined): string {
  if (!scope || scope.kind === 'unavailable') return "Unconfirmed";
  if (scope.kind === 'mixed') {
    const environments = scope.environments
      .map((environment) => environment.toLocaleUpperCase("en-US"))
      .join(', ');
    return environments || "Unconfirmed";
  }
  return scope.environments[0]?.toLocaleUpperCase("en-US") ?? "Unconfirmed";
}

export function sourceAvailability(overview: Pick<Overview, 'sources'>): SourceAvailability {
  if (overview.sources.length === 0) return 'unestablished';
  const unavailable = overview.sources.filter((source) => source.status === 'unavailable').length;
  if (unavailable === 0) return 'available';
  if (unavailable === overview.sources.length) return 'unavailable';
  return 'partial';
}

export function sourceAvailabilityCopy(overview: Pick<Overview, 'sources'>): SourceAvailabilityCopy | null {
  switch (sourceAvailability(overview)) {
    case 'available':
      return null;
    case 'partial':
      return {
        label: "Some sources unavailable",
        detail: "The observation does not cover all sources: displayed counts do not prove completeness.",
      };
    case 'unavailable':
      return {
        label: "Sources unavailable",
        detail: "The last retained observation remains visible, but neither a current state nor complete counts can be confirmed.",
      };
    case 'unestablished':
      return {
        label: "Source availability not established",
        detail: "The received information cannot confirm observation coverage.",
      };
  }
}
