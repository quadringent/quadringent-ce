export function nativeRecordFixture() {
  const digest = 'a'.repeat(64);
  const dataset = { row_count: 2, byte_count: 200, sha256: digest, rows_sha256: digest,
    started_at: '2026-10-16T12:00:00Z', completed_at: '2026-10-16T12:00:01Z',
    unique_count: 2, duplicate_count: 0, conflicting_identity_count: 0, exact_replay_count: 0 };
  const comparison = { expected_count: 2, actual_count: 2, missing_count: 0, unexpected_count: 0, different_count: 0, equal: true };
  return { id: 'nv_fixture', pipeline_id: 'pl_fixture', lifecycle: 'finished', outcome: 'incomplete',
    reason_code: 'native_authority_unavailable', context_generation: 1, context_sha256: digest, scope_sha256: digest,
    context_validity: 'same_context', context_enforcement: 'enabled', observed_at: '2026-10-16T12:00:01Z',
    created_at: '2026-10-16T12:00:00Z', finished_at: '2026-10-16T12:00:02Z', age_seconds: 2,
    result_manifest: { format_version: 'quadringent-native-observed-result-v1', scope_sha256: digest,
      context_sha256: digest, context_generation: 1, native_admission: 'not_evaluated', native_cdc_qualified: false,
      streams_replayed: 7, validated_observations: ['destination_identity', 'receiver_sampled_ranges'],
      missing_authorities: ['platform', 'source_provenance', 'receiver_chain', 'physical_inventory', 'stable_cut'],
      comparison: { format_version: 'quadringent-native-table-comparison-v1', scope_sha256: digest, state: 'verified',
        datasets: Object.fromEntries(['source', 'snapshot', 'journal', 'positions', 'raw', 'history', 'mirror'].map(key => [key, { ...dataset }])),
        comparisons: Object.fromEntries(['journal_positions', 'raw', 'history', 'source', 'mirror'].map(key => [key, { ...comparison }])),
        checks: { snapshot_rows_complete: true, journal_row_images_reconstructed: true, all_streams_complete: true,
          all_fields_compared: true, no_logical_duplicates: true }, transaction_consistency_verified: false,
      } },
  };
}
