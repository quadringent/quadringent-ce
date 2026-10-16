export const NATIVE_DATASETS = ['source', 'snapshot', 'journal', 'positions', 'raw', 'history', 'mirror'] as const;
const OBSERVATIONS = ['destination_identity', 'receiver_sampled_ranges', 'native_source_observations'];
const AUTHORITIES = ['platform', 'source_provenance', 'receiver_chain', 'physical_inventory', 'stable_cut', 'destination_identity'];

export interface NativeCapabilities {
  readonly available: boolean;
  readonly mode: 'observation' | 'admission' | null;
  readonly reasonCode: 'not_configured' | 'native_context_unavailable' | null;
}
export interface NativeDataset {
  readonly name: typeof NATIVE_DATASETS[number];
  readonly rowCount: number;
  readonly duplicateCount: number;
  readonly completedAt: string;
}
export interface NativeResult {
  readonly kind: 'observed' | 'admitted';
  readonly comparisonState: 'verified' | 'divergent';
  readonly datasets: readonly NativeDataset[];
  readonly comparisons: readonly { readonly name: string; readonly equal: boolean; readonly missing: number; readonly unexpected: number; readonly different: number }[];
  readonly checks: readonly { readonly name: string; readonly passed: boolean }[];
  readonly validatedObservations: readonly string[];
  readonly missingAuthorities: readonly string[];
}
export interface NativeVerification {
  readonly id: string;
  readonly pipelineId: string;
  readonly lifecycle: 'preparing' | 'running' | 'finished';
  readonly outcome: 'verified' | 'divergent' | 'incomplete' | null;
  readonly contextValidity: 'same_context' | 'superseded' | 'tracking_unavailable';
  readonly contextSha256: string;
  readonly contextGeneration: number;
  readonly scopeSha256: string | null;
  readonly observedAt: string | null;
  readonly createdAt: string;
  readonly result: NativeResult | null;
}

function requireValue(ok: unknown): asserts ok {
  if (!ok) throw new Error('Invalid native verification response.');
}
function record(value: unknown): Record<string, any> {
  requireValue(typeof value === 'object' && value !== null && !Array.isArray(value));
  return value as Record<string, any>;
}
function count(value: unknown): number {
  requireValue(typeof value === 'number' && Number.isSafeInteger(value) && value >= 0);
  return value;
}
function text(value: unknown): string {
  requireValue(typeof value === 'string' && value.length > 0 && value.length <= 160);
  return value;
}
function time(value: unknown): string {
  const result = text(value);
  requireValue(/^\d{4}-\d\d-\d\dT.+(?:Z|[+-]\d\d:\d\d)$/.test(result) && Number.isFinite(Date.parse(result)));
  return result;
}
function digest(value: unknown): string {
  requireValue(typeof value === 'string' && /^[a-f0-9]{64}$/.test(value));
  return value;
}
function names(value: unknown, allowed: readonly string[]): readonly string[] {
  requireValue(Array.isArray(value) && value.length <= allowed.length && new Set(value).size === value.length
    && value.every(v => typeof v === 'string' && allowed.includes(v)));
  return value;
}

export function parseNativeCapabilities(input: unknown): NativeCapabilities {
  const value = record(input);
  requireValue(typeof value.available === 'boolean' && ['observation', 'admission', null].includes(value.mode)
    && ['not_configured', 'native_context_unavailable', null].includes(value.reason_code)
    && (value.available ? value.mode !== null && value.reason_code === null : value.mode === null && value.reason_code !== null));
  return { available: value.available, mode: value.mode, reasonCode: value.reason_code };
}

export function parseNativeVerification(input: unknown, pipelineId: string): NativeVerification | null {
  if (input === null) return null;
  const value = record(input);
  requireValue(value.pipeline_id === pipelineId && ['preparing', 'running', 'finished'].includes(value.lifecycle)
    && ['verified', 'divergent', 'incomplete', null].includes(value.outcome)
    && ['same_context', 'superseded', 'tracking_unavailable'].includes(value.context_validity));
  requireValue(['enabled', 'unavailable'].includes(value.context_enforcement)
    && (value.context_validity === 'tracking_unavailable') === (value.context_enforcement === 'unavailable')
    && (value.lifecycle === 'finished' ? value.outcome !== null : value.outcome === null && value.result_manifest === null));
  let result: NativeResult | null = null;
  if (value.result_manifest !== null) {
    const manifest = record(value.result_manifest);
    const observed = manifest.format_version === 'quadringent-native-observed-result-v1';
    requireValue(observed || manifest.format_version === 'quadringent-native-admitted-result-v1');
    requireValue(digest(manifest.scope_sha256) === digest(value.scope_sha256)
      && digest(manifest.context_sha256) === digest(value.context_sha256)
      && count(manifest.context_generation) === count(value.context_generation));
    if (!observed) requireValue(typeof manifest.policy_revision === 'string' && manifest.policy_revision.length > 0 && manifest.policy_revision.length <= 160);
    if (observed) {
      requireValue(value.outcome === 'incomplete' && manifest.native_admission === 'not_evaluated'
        && manifest.native_cdc_qualified === false && manifest.streams_replayed === 7);
    }
    const comparison = record(manifest.comparison);
    requireValue(comparison.format_version === 'quadringent-native-table-comparison-v1'
      && digest(comparison.scope_sha256) === value.scope_sha256 && ['verified', 'divergent'].includes(comparison.state));
    requireValue(observed || value.outcome === comparison.state);
    const datasets = record(comparison.datasets);
    requireValue(Object.keys(datasets).length === 7);
    const rows = NATIVE_DATASETS.map(name => {
      const row = record(datasets[name]);
      digest(row.sha256); digest(row.rows_sha256); count(row.byte_count);
      return { name, rowCount: count(row.row_count), duplicateCount: count(row.duplicate_count), completedAt: time(row.completed_at) };
    });
    const comparisons = ['journal_positions', 'raw', 'history', 'source', 'mirror'].map(name => {
      const row = record(record(comparison.comparisons)[name]);
      const missing = count(row.missing_count), unexpected = count(row.unexpected_count), different = count(row.different_count);
      count(row.expected_count); count(row.actual_count);
      requireValue(typeof row.equal === 'boolean' && row.equal === (missing === 0 && unexpected === 0 && different === 0));
      return { name, equal: row.equal, missing, unexpected, different };
    });
    const checks = record(comparison.checks);
    const checkNames = ['snapshot_rows_complete', 'journal_row_images_reconstructed', 'all_streams_complete', 'all_fields_compared', 'no_logical_duplicates'];
    requireValue(checkNames.every(name => typeof checks[name] === 'boolean'));
    requireValue((comparison.state === 'verified') === (comparisons.every(row => row.equal) && checkNames.every(name => checks[name])));
    const validatedObservations = observed ? names(manifest.validated_observations, OBSERVATIONS) : [];
    const missingAuthorities = observed ? names(manifest.missing_authorities, AUTHORITIES) : [];
    requireValue(!observed || AUTHORITIES.slice(0, 5).every(name => missingAuthorities.includes(name)));
    result = { kind: observed ? 'observed' : 'admitted', comparisonState: comparison.state,
      datasets: rows, comparisons, checks: checkNames.map(name => ({ name, passed: checks[name] })), validatedObservations, missingAuthorities };
  }
  return { id: text(value.id), pipelineId, lifecycle: value.lifecycle, outcome: value.outcome,
    contextValidity: value.context_validity,
    contextSha256: digest(value.context_sha256), contextGeneration: count(value.context_generation),
    scopeSha256: value.scope_sha256 === null ? null : digest(value.scope_sha256), observedAt: value.observed_at === null ? null : time(value.observed_at),
    createdAt: time(value.created_at), result };
}
