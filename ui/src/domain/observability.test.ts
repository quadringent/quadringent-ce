import assert from 'node:assert/strict';
import test from 'node:test';

import { observabilityFixture } from './observability.testFixtures.ts';
import {
  observabilityBoundary,
  observabilityIsCurrent,
  orderedSloChecks,
  sloCheckLabel,
  sloReasonLabel,
  sloValueLabel,
} from './observability.ts';

test('SLO copy stays public, localized and does not expose unknown raw identifiers', () => {
  assert.match(sloReasonLabel('metering_window_1788156000_1788242400_2'), /UTC.*2 observations/);
  assert.equal(sloReasonLabel('metering_window_1788156000_1788242401_2'), "Reason not published");
  assert.equal(sloCheckLabel('snowpipe_queue'), "Pending Snowpipe queue");
  assert.equal(sloCheckLabel('secret_internal_probe'), "Check removed");
  assert.equal(sloReasonLabel('threshold_exceeded'), "Limit exceeded");
  assert.equal(sloReasonLabel('jdbc_operator_secret'), "Reason not published");
  assert.equal(sloValueLabel('OPERATOR_SECRET', 'state'), "State not published");
  assert.equal(
    sloValueLabel(['RUNNING', 'OPERATOR_SECRET'], 'state'),
    "In progress or State not published",
  );
});

test('SLO values preserve exact measurements with readable singular units', () => {
  assert.equal(sloValueLabel(1, 'warehousecredits/delayed24h'), "1 credit warehouse / delayed 24 h");
  assert.equal(sloValueLabel(1, 'sequences'), '1 position');
  assert.equal(sloValueLabel(0, 'errors'), '0 errors');
  assert.equal(sloValueLabel(1, 'files'), '1 file');
  assert.equal(sloValueLabel(2, 'files'), '2 files');
  assert.equal(sloValueLabel(1, 'requests/24h'), '1 request / 24 h');
  assert.equal(sloValueLabel(2, 'requests/24h'), '2 requests / 24 h');
  assert.equal(sloValueLabel('RUNNING', 'state'), "In progress");
  assert.equal(sloValueLabel(['RUNNING', 'STOPPED_BUDGET'], 'state'), "In progress or Planned stop");
  assert.equal(sloValueLabel(null, 'files'), "Not measured");
});

test('controls follow the operator path and current eligibility is non-compensatory', () => {
  const live = observabilityFixture({ status: 'pass' });
  const ordered = orderedSloChecks([...live.checks].reverse());

  assert.equal(ordered[0]?.id, 'capture_freshness');
  assert.equal(ordered.at(-1)?.id, 'observability_freshness');
  assert.equal(observabilityIsCurrent(live, true), true);
  assert.equal(observabilityIsCurrent({ ...live, quality: { ...live.quality, freshness: 'stale' } }, true), false);
  assert.equal(observabilityIsCurrent(live, false), false);
  assert.equal(observabilityBoundary({ ...live, quality: { ...live.quality, freshness: 'stale' } }), "Check observation expired");
});
