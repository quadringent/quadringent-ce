import assert from 'node:assert/strict';
import test from 'node:test';
import { shouldRedirectToWizard, nativeLandingRoute } from './useFirstRunRedirect.ts';

test('redirects only from the overview/index home with zero sources declared', () => {
  assert.equal(shouldRedirectToWizard('overview', 0), true);
  assert.equal(shouldRedirectToWizard('index', 0), true);
});

test('never redirects once at least one source exists', () => {
  assert.equal(shouldRedirectToWizard('overview', 1), false);
  assert.equal(shouldRedirectToWizard('overview', 5), false);
});

test('never redirects from any other screen, even with zero sources', () => {
  assert.equal(shouldRedirectToWizard('pipelines', 0), false);
  assert.equal(shouldRedirectToWizard('setup', 0), false);
  assert.equal(shouldRedirectToWizard('cockpit', 0), false);
  assert.equal(shouldRedirectToWizard('wizard', 0), false);
});


test('native home routes choose cockpit with sources and wizard without sources', () => {
  for (const name of ['overview', 'index'] as const) {
    assert.deepEqual(nativeLandingRoute(name, 1), { name: 'cockpit' });
    assert.deepEqual(nativeLandingRoute(name, 0), { name: 'wizard', step: 'source' });
  }
});

test('setup enters native wizard while other explicit routes remain stable', () => {
  assert.deepEqual(nativeLandingRoute('setup', 1), { name: 'wizard', step: 'source' });
  assert.deepEqual(nativeLandingRoute('setup', 0), { name: 'wizard', step: 'source' });
  for (const name of ['pipelines', 'cockpit', 'wizard'] as const) assert.equal(nativeLandingRoute(name, 1), null);
});

test('unavailable or invalid source count never redirects', () => {
  for (const count of [undefined, -1, NaN]) assert.equal(nativeLandingRoute('overview', count), null);
});
