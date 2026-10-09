import assert from 'node:assert/strict';
import test from 'node:test';

import { buildMeasuredUsage, usageView } from './usage.ts';
import type { Pipeline } from './controlPlane.ts';

const observedAt = '2026-08-28T08:33:56.470000+00:00';

test('an estimate cannot render as a measured value', () => {
  const view = usageView({
    kind: 'estimate',
    label: 'Coût mensuel projeté',
    value: 4.4,
    currency: 'USD',
    methodology: "Assumption: constant volume over 30 days.",
    assumptions: ['Prix public sans remise négociée'],
    asOf: '2026-08-28T00:00:00Z',
    region: 'us-east-1',
  });

  assert.equal(view.kind, 'estimate');
  assert.equal(view.badge, "Estimate");
  assert.match(view.methodology, /assumption/i);
  assert.equal(view.asOf, '2026-08-28T00:00:00Z');
  assert.equal(view.region, 'us-east-1');
  assert.notEqual(view.badge, "Measured");
});

test('an estimate requires methodology assumptions date and region', () => {
  assert.throws(
    () => usageView({
      kind: 'estimate',
      label: 'Coût',
      value: 4.4,
      currency: 'USD',
      methodology: '',
      assumptions: [],
      asOf: '',
      region: '',
    }),
    /methodology/i,
  );
});

test('measured counters keep unit source observation and provenance', () => {
  const [pipelineUsage] = buildMeasuredUsage([
    pipelineFixture('alpha', {
      events_published: 1_793,
      payload_bytes_published: 622_091,
      events_in_target: null,
    }),
  ]);

  assert.equal(pipelineUsage?.pipelineId, 'alpha');
  assert.deepEqual(
    pipelineUsage?.metrics.map((metric) => ({
      key: metric.key,
      badge: metric.badge,
      unit: metric.unit,
      source: metric.source,
      observedAt: metric.observedAt,
      provenance: metric.provenance,
    })),
    [
      {
        key: 'events_published',
        badge: "Measured",
        unit: "events",
        source: 'control plane / alpha / counters.events_published',
        observedAt,
        provenance: 'simulation',
      },
      {
        key: 'payload_bytes_published',
        badge: "Measured",
        unit: "bytes",
        source: 'control plane / alpha / counters.payload_bytes_published',
        observedAt,
        provenance: 'simulation',
      },
    ],
  );
});

test('heterogeneous and cross-pipeline counters are never added together', () => {
  const usage = buildMeasuredUsage([
    pipelineFixture('alpha', { events_published: 10, payload_bytes_published: 100 }),
    pipelineFixture('beta', { events_published: 20, payload_bytes_published: 200 }),
  ]);

  assert.equal(usage.length, 2);
  assert.deepEqual(usage.map((item) => item.pipelineId), ['alpha', 'beta']);
  assert.deepEqual(usage[0]?.metrics.map((metric) => metric.value), [10, 100]);
  assert.deepEqual(usage[1]?.metrics.map((metric) => metric.value), [20, 200]);
  assert.equal(usage.flatMap((item) => item.metrics).some((metric) => metric.value === 330), false);
});

test('null and semantically unknown counters do not become measured usage', () => {
  const [usage] = buildMeasuredUsage([
    pipelineFixture('alpha', {
      events_in_target: null,
      undocumented_business_rows: 42,
    }),
  ]);

  assert.deepEqual(usage?.metrics, []);
});

function pipelineFixture(id: string, counters: Readonly<Record<string, number | null>>): Pipeline {
  return {
    id,
    environment: 'dev',
    status: 'degraded',
    quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: 'simulation' },
    summary: "Delivery Snowflake non proven",
    observedAt,
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'Signal présent' },
      { id: 'capture', status: 'healthy', observedAt, headline: 'Capture observée', detail: 'Signal présent' },
      { id: 'raw', status: 'healthy', observedAt, headline: "Raw observed", detail: 'Signal présent' },
      { id: 'load', status: 'unknown', observedAt: null, headline: 'Load inconnu', detail: "No observed" },
      { id: 'destination', status: 'unknown', observedAt: null, headline: 'Destination inconnue', detail: "Not observed" },
    ],
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters,
    incident: null,
  };
}
