import type { Observability, SloCheck, SloSignalStatus, SloValue } from './controlPlane.ts';

const numberFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 });

const checkLabels: Readonly<Record<string, string>> = {
  capture_freshness: "Capture freshness",
  capture_state: "Capture state",
  capture_errors: "Capture errors",
  checkpoint_lag: "Recovery point lag",
  s3_freshness: "S3 storage freshness",
  s3_requests: "S3 storage requests over 24 h",
  snowpipe_queue: "Pending Snowpipe queue",
  canonical_freshness: "Canonical table freshness",
  delivery_latency_p95: "Delivery latency p95",
  delivery_latency_p99: "Delivery latency p99",
  reconciliation: "End-to-end reconciliation",
  snowflake_credits: "Snowflake credits over 24 h",
  observability_freshness: "Check freshness",
};

const stageLabels: Readonly<Record<string, string>> = {
  capture: "Reader",
  checkpoint: "Recovery point",
  s3: "S3 storage",
  cost: "Observed costs",
  snowpipe: 'Snowpipe',
  canonical: "Canonical table",
  delivery: "Delivery",
  destination: 'Destination',
};

const reasonLabels: Readonly<Record<string, string>> = {
  within_threshold: "Within the configured limit",
  running: "Capturing",
  planned_stop: "Planned stop as configured",
  threshold_exceeded: "Limit exceeded",
  freshness_exceeded: "Freshness limit exceeded",
  count_mismatch: "Reconciliation difference",
  capture_fail_closed: "Capture stopped safely",
  measurement_missing: "Measurement absent",
  run_missing: "Capture state absent",
  run_state_unknown: "Unrecognized capture state",
  position_missing: "Reader position absent",
  receiver_chain_required: "Reception chain to confirm",
  proof_missing: "Destination evidence absent",
  counts_missing: "Incomplete reconciliation counts",
  clock_untrusted: "Clock unreliable",
};

const valueLabels: Readonly<Record<string, string>> = {
  RUNNING: "In progress",
  PAUSED_SOURCE: "Source paused",
  STOPPED_BUDGET: "Planned stop",
  STOPPED_FAIL_CLOSED: "Stopped safely",
  STOPPED_AUTH_BLOCKED: "Authentication blocked",
};

const checkOrder = Object.keys(checkLabels);

export function sloCheckLabel(id: string): string {
  return checkLabels[id] ?? "Check removed";
}

export function sloStageLabel(stage: string): string {
  return stageLabels[stage] ?? "Stage removed";
}

export function sloReasonLabel(reason: string): string {
  const metering = /^metering_window_(\d{10})_(\d{10})_([1-9]\d{0,3})$/.exec(reason);
  if (metering) {
    const [, start, end, count] = metering;
    if (Number(end) - Number(start) === 86400 && Number(count) <= 1000) {
      const format = (seconds: string) => new Intl.DateTimeFormat("en-US", { hourCycle: 'h23',
        timeZone: 'UTC', dateStyle: 'short', timeStyle: 'short',
      }).format(new Date(Number(seconds) * 1000));
      return `From ${format(start)} to ${format(end)} UTC · ${count} observation${count === '1' ? '' : 's'} horaire${count === '1' ? '' : 's'}`;
    }
  }
  return reasonLabels[reason] ?? "Reason not published";
}

export function sloSignalLabel(status: SloSignalStatus): string {
  if (status === 'pass') return "Compliant";
  if (status === 'breach') return "Exceeded";
  return "To measure";
}

export function sloValueLabel(value: SloValue, unit: string | null): string {
  if (value === null) return "Not measured";
  if (typeof value !== 'number' && typeof value !== 'string') {
    return value.map((item) => publicValueLabel(item, unit)).join(" or ");
  }
  const rendered = typeof value === 'number' ? numberFormat.format(value) : publicValueLabel(value, unit);
  const renderedUnit = unit ? unitLabel(unit, typeof value === 'number' ? value : null) : null;
  return renderedUnit ? `${rendered} ${renderedUnit}` : rendered;
}

export function orderedSloChecks(checks: readonly SloCheck[]): readonly SloCheck[] {
  return [...checks].sort((left, right) => {
    const leftIndex = checkOrder.indexOf(left.id);
    const rightIndex = checkOrder.indexOf(right.id);
    return (leftIndex < 0 ? Number.MAX_SAFE_INTEGER : leftIndex)
      - (rightIndex < 0 ? Number.MAX_SAFE_INTEGER : rightIndex);
  });
}

export function observabilityIsCurrent(observability: Observability, currentEvidence: boolean): boolean {
  return currentEvidence
    && observability.status !== 'unavailable'
    && observability.quality.coverage === 'complete'
    && observability.quality.freshness === 'fresh'
    && observability.quality.evidenceKind === 'live';
}

export function observabilityBoundary(observability: Observability): string {
  if (observability.status === 'unavailable') return "Checks not published";
  if (observability.quality.coverage !== 'complete') return "Partial check coverage";
  if (observability.quality.freshness === 'stale') return "Check observation expired";
  if (observability.quality.freshness === 'clock_untrusted') return "Check freshness not established";
  if (observability.quality.evidenceKind === 'simulation') return "Checks limited to the simulation";
  if (observability.quality.evidenceKind === 'historical') return "Historical snapshot checks";
  return "Fresh check observation";
}

function publicValueLabel(value: string, unit: string | null): string {
  return valueLabels[value] ?? (unit === 'state' ? "State not published" : "Value not published");
}

function unitLabel(unit: string, value: number | null): string | null {
  const singular = value === 1;
  switch (unit) {
    case 'seconds': return 's';
    case 'requests/24h': return `${singular ? 'request' : 'requests'} / 24 h`;
    case 'credits/24h': return `${singular ? "credit" : "credits"} / 24 h`;
    case 'warehousecredits/delayed24h': return `${singular ? "credit" : "credits"} warehouse / delayed 24 h`;
    case 'state': return null;
    case 'errors': return singular ? 'error' : 'errors';
    case 'files': return singular ? 'file' : 'files';
    case 'events': return singular ? "event" : "events";
    case 'sequences': return singular ? 'position' : 'positions';
    default: return unit;
  }
}
