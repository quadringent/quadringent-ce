import assert from 'node:assert/strict';
import test from 'node:test';

import { buildCounterMetrics, buildPipelineLead, buildPipelineMissingCopy, buildPriorityMeta, buildStatusNarrative, buildWorkspaceSummary } from './productViewModels.ts';

test('loading and failed summaries never invent zero incidents or recoveries', () => {
  const loading = buildWorkspaceSummary({ status: 'loading', connection: 'connecting' }, []);
  const failed = buildWorkspaceSummary({ status: 'failed', connection: 'offline', message: 'offline' }, []);

  assert.equal(loading.incidents.value, "Unknown");
  assert.equal(loading.recoveries.value, "Unknown");
  assert.equal(failed.incidents.value, "Unknown");
  assert.equal(failed.recoveries.value, "Unknown");
  assert.equal(loading.empty.title, "Data unavailable");
  assert.equal(failed.empty.title, "Data unavailable");
  assert.ok(!loading.incidents.value.includes('0'));
  assert.ok(!failed.recoveries.value.includes('0'));
});

test('degraded narratives expose the cached age and safe message', () => {
  const narrative = buildStatusNarrative(
    {
      status: 'degraded',
      connection: 'reconnecting',
      pipelines: [],
      lastSuccessAt: new Date('2026-08-28T10:00:00Z'),
      message: "Flow temps réel unavailable ; reconnexion in progress.",
    },
    new Date('2026-08-28T10:03:00Z'),
  );

  assert.match(narrative, /3 min/);
  assert.match(narrative, /reconnexion/i);
});

test('missing pipeline copies do not mention an absent id during loading or failure', () => {
  const loading = buildPipelineMissingCopy({ status: 'loading', connection: 'connecting' }, 'dev-cntr');
  const failed = buildPipelineMissingCopy({ status: 'failed', connection: 'offline', message: 'offline' }, 'dev-cntr');

  assert.equal(loading.title, "Data unavailable");
  assert.equal(failed.title, "Data unavailable");
  assert.doesNotMatch(loading.description, /absent/i);
  assert.doesNotMatch(failed.description, /absent/i);
});

test('a missing pipeline is confirmed only by a live ready snapshot with exhaustive sources', () => {
  const available = overviewWithSources(['available']);
  const unavailable = overviewWithSources(['unavailable']);
  const partial = overviewWithSources(['available', 'unavailable']);
  const unmapped = overviewWithSources([]);
  const confirmed = buildPipelineMissingCopy(readyState(available), 'dev-cntr');
  const uncertainStates = [
    readyState(unavailable),
    readyState(partial),
    readyState(unmapped),
    {
      status: 'degraded' as const,
      connection: 'reconnecting' as const,
      overview: available,
      pipelines: [],
      lastSuccessAt: new Date('2026-08-28T10:00:00Z'),
      message: "Retained snapshot",
    },
  ];

  assert.equal(confirmed.title, "Pipeline not found");
  assert.match(confirmed.description, /confirmed observation does not contain this pipeline/i);
  for (const state of uncertainStates) {
    const copy = buildPipelineMissingCopy(state, 'dev-cntr');
    assert.equal(copy.title, "Pipeline presence to confirm");
    assert.match(copy.description, /cannot confirm/i);
    assert.doesNotMatch(`${copy.title} ${copy.description}`, /pipeline absent|does not contain this pipeline/i);
  }
});

test('pipeline leads stay availability-first while loading or failed', () => {
  assert.equal(
    buildPipelineLead({ status: 'loading', connection: 'connecting' }, null),
    "Verification in progress — the console cannot confirm this pipeline yet.",
  );
  assert.equal(
    buildPipelineLead({ status: 'failed', connection: 'offline', message: 'offline' }, null),
    "Data unavailable — the console cannot confirm this pipeline yet.",
  );
  assert.equal(
    buildPipelineLead(readyState(overviewWithSources(['available'])), null),
    "The confirmed observation does not contain this pipeline.",
  );
});

test('priority meta stays availability-first while loading or failed', () => {
  assert.equal(buildPriorityMeta({ status: 'loading', connection: 'connecting' }, 0), "Verification in progress");
  assert.equal(
    buildPriorityMeta({ status: 'failed', connection: 'offline', message: 'offline' }, 0),
    "Data unavailable — no observation",
  );
  assert.equal(
    buildPriorityMeta({ status: 'ready', connection: 'live', pipelines: [], lastSuccessAt: new Date('2026-08-28T10:00:00Z') }, 0),
    "No recovery observed",
  );
});

test('destination counter labels disclose event-window scope instead of business table totals', () => {
  const metrics = buildCounterMetrics({ events_in_target: 1064, duplicates_in_target: 0 });
  assert.match(metrics[0]!.label, /events/i);
  assert.match(metrics[0]!.label, /window/i);
  assert.match(metrics[1]!.label, /window/i);
  assert.equal(metrics[1]!.value, '0');
});

test('counter metrics keep every entry visible including null counters', () => {
  const metrics = buildCounterMetrics({
    a: 1,
    b: 2,
    c: 3,
    d: 4,
    e: null,
  });

  assert.equal(metrics.length, 5);
  assert.equal(metrics[4]?.label, 'e');
  assert.equal(metrics[4]?.value, "Unknown");
  assert.equal(metrics[4]?.unknownReason, "Counter to confirm");
});

function overviewWithSources(statuses: readonly ('available' | 'unavailable')[]) {
  return {
    revision: 1,
    generatedAt: '2026-08-28T10:00:00Z',
    scope: { kind: statuses.length ? 'single' as const : 'unavailable' as const, environments: statuses.length ? ['dev'] : [] },
    pipelines: [],
    sources: statuses.map((status, index) => ({
      id: `dev-${index}`,
      environment: 'dev',
      evidenceKind: 'live' as const,
      status,
      error: status === 'available' ? null : 'offline',
    })),
  };
}

function readyState(overview: ReturnType<typeof overviewWithSources>) {
  return {
    status: 'ready' as const,
    connection: 'live' as const,
    overview,
    pipelines: [],
    lastSuccessAt: new Date('2026-08-28T10:00:00Z'),
  };
}
