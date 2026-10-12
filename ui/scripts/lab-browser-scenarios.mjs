#!/usr/bin/env node
/**
 * Browser scenarios of the local laboratory (docs/product/local-lab.md).
 *
 * Renders the REAL built frontend (ui/dist, same origin) against the REAL v2
 * control plane served by `scripts/quadringent_lab.py`; IBM i, Snowflake and
 * Kubernetes are replaced only at the product's injectable boundaries. No
 * browser response is mocked or intercepted.
 *
 *   npm --prefix ui run build
 *   node ui/scripts/lab-browser-scenarios.mjs --screenshots /tmp/lab-shots
 *
 * Options
 *   --screenshots DIR   save one PNG per check and colour scheme
 *   --attach URL        use a laboratory that is already running (loopback only);
 *                       scenario 10 still starts its own throwaway instance
 *   --only 1,11,13      run only these scenario numbers
 *   --skip-slow         skip scenario 16 (waits for the 30 s action timeout)
 *
 * Environment
 *   QUADRINGENT_PLAYWRIGHT_MODULE   playwright module to import (default `playwright`)
 *   QUADRINGENT_BROWSER_CHANNEL     browser channel (default `chrome`)
 *   QUADRINGENT_BROWSER_EXECUTABLE  explicit browser executable (overrides the channel)
 *   QUADRINGENT_LAB_PYTHON          python interpreter (default `.venv/bin/python`, else python3)
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const { chromium } = await import(process.env.QUADRINGENT_PLAYWRIGHT_MODULE || 'playwright');

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..', '..');
const labScript = join(repoRoot, 'scripts', 'quadringent_lab.py');
const python = process.env.QUADRINGENT_LAB_PYTHON
  || (existsSync(join(repoRoot, '.venv', 'bin', 'python')) ? join(repoRoot, '.venv', 'bin', 'python') : 'python3');

// ---------------------------------------------------------------- arguments
const argv = process.argv.slice(2);
const option = (name) => {
  const index = argv.indexOf(name);
  return index >= 0 ? argv[index + 1] : undefined;
};
const screenshotsDir = option('--screenshots');
const attachUrl = option('--attach');
const only = option('--only')?.split(',').map(Number);
const skipSlow = argv.includes('--skip-slow');
if (screenshotsDir) mkdirSync(screenshotsDir, { recursive: true });
if (attachUrl) {
  const parsed = new URL(attachUrl);
  assert.equal(parsed.protocol, 'http:');
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname), 'Loopback URL required');
}

const OPERATOR = { email: 'operator@lab.invalid', password: 'lab-fixture-passphrase' };
const ORDERS = 'pl_lab_orders';
const CUSTOMERS = 'pl_lab_customers';
const CANARIES = [
  'canary-host-4f9a2c', 'CANARYUSR', 'CANARY-SECRET-4f9a2c', 'canary-account-4f9a2c',
  'CANARY-ROW-VALUE-7731', 'CANARY-LOG-SECRET-5512', 'CANARY-EVIDENCE-SECRET-9024', 'CANARY-TABLE-COMMENT-3318',
];
// The signed-in operator may see their own host/account labels on screen; secrets and row data never.
const SECRET_CANARIES = CANARIES.filter((value) => /SECRET|ROW-VALUE|COMMENT/.test(value));

// ------------------------------------------------------------ lab processes
function startLab({ scenario = 'baseline', allowShutdown = false } = {}) {
  const args = [labScript, 'start', '--scenario', scenario, '--port', '0'];
  if (allowShutdown) args.push('--allow-shutdown');
  const child = spawn(python, args, { cwd: repoRoot, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  return new Promise((resolveReady, reject) => {
    let buffer = '';
    const timer = setTimeout(() => reject(new Error(`laboratory did not become ready: ${stderr}`)), 60_000);
    child.on('exit', (code) => { clearTimeout(timer); reject(new Error(`laboratory exited (${code}): ${stderr}`)); });
    child.stdout.on('data', (chunk) => {
      buffer += chunk;
      const match = /READY (http:\/\/127\.0\.0\.1:\d+\/)/.exec(buffer);
      if (match) {
        clearTimeout(timer);
        child.removeAllListeners('exit');
        resolveReady({ url: match[1], child });
      }
    });
  });
}

async function stopLab(lab) {
  if (!lab?.child || lab.child.exitCode !== null) return;
  const exited = new Promise((done) => lab.child.once('exit', done));
  lab.child.kill('SIGTERM');
  await Promise.race([exited, new Promise((done) => setTimeout(done, 15_000))]);
}

const labApi = async (base, path, init) => {
  const response = await fetch(new URL(path, base), {
    ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  assert.ok(response.ok, `${path} -> ${response.status}`);
  return response.json();
};
const activate = (base, scenario) => labApi(base, '/lab/scenario', { method: 'PUT', body: JSON.stringify({ name: scenario }) });
const ledger = async (base, boundary) => (await labApi(base, `/lab/ledger?boundary=${boundary}`)).items;

// -------------------------------------------------------------------- pages
/** HTTP errors a page may legitimately produce, each with its reason. */
const EXPLAINED = [
  { status: 404, path: /^\/v1\//, reason: 'legacy v1 shell polls /v1; the laboratory serves /v2 only' },
  { status: 409, path: /^\/v2\/pipelines\/[^/]+\/actions\/(restart_initial_copy|remove|replay)$/,
    reason: 'sensitive actions answer 409 pending_confirmation_required by design' },
  { status: 401, path: /^\/v2\//, reason: 'signed-out requests are answered 401 and redirected to sign-in', phase: 'signedOut' },
];

class Watch {
  constructor(page) {
    this.unexplained = [];
    this.phase = 'signedOut';
    this.expectOutage = false;
    page.on('pageerror', (error) => this.unexplained.push(`pageerror: ${error.message}`));
    page.on('response', (response) => {
      if (response.status() < 400) return;
      const url = new URL(response.url());
      const known = EXPLAINED.find((rule) => rule.status === response.status() && rule.path.test(url.pathname)
        && (!rule.phase || rule.phase === this.phase));
      if (!known) this.unexplained.push(`HTTP ${response.status()} ${url.pathname}`);
    });
    page.on('requestfailed', (request) => {
      const reason = request.failure()?.errorText ?? '';
      if (reason === 'net::ERR_ABORTED') return;
      if (this.expectOutage) return;
      this.unexplained.push(`requestfailed ${new URL(request.url()).pathname}: ${reason}`);
    });
    page.on('console', (message) => {
      if (message.type() !== 'error') return;
      // Browser-generated lines for HTTP errors are accounted for through the response rules above.
      if (/^Failed to load resource/.test(message.text())) return;
      if (this.expectOutage && /ERR_CONNECTION_REFUSED|Failed to fetch/.test(message.text())) return;
      this.unexplained.push(`console.error: ${message.text()}`);
    });
  }
  take() {
    const found = this.unexplained.splice(0);
    return found;
  }
}

async function signIn(page, watch, credentials = OPERATOR) {
  await page.getByLabel('Email address').fill(credentials.email);
  await page.getByLabel('Password').fill(credentials.password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.waitForFunction(() => !location.hash.startsWith('#/login'));
  watch.phase = 'signedIn';
}

async function open(page, base, hash) {
  await page.goto(new URL(`/${hash}`, base).href);
  // A hash-only change is a same-document navigation: reload so every screen reads the
  // current service state, exactly as an operator pressing the browser's refresh would.
  await page.reload();
  await page.waitForLoadState('domcontentloaded');
}

const bodyText = (page) => page.locator('body').innerText();

async function settle(page) {
  await page.waitForFunction(() => !/Quadringent is querying the service/.test(document.body.innerText), null, { timeout: 15_000 });
}

/** WCAG contrast ratio of the main text against its effective background. */
async function contrast(page) {
  return page.evaluate(() => {
    const parse = (value) => {
      const m = /rgba?\(([^)]+)\)/.exec(value);
      if (!m) return null;
      const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
      return { r, g, b, a };
    };
    const background = (element) => {
      for (let node = element; node; node = node.parentElement) {
        const color = parse(getComputedStyle(node).backgroundColor);
        if (color && color.a > 0.95) return color;
      }
      return { r: 255, g: 255, b: 255, a: 1 };
    };
    const luminance = ({ r, g, b }) => {
      const channel = (v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const target = document.querySelector('h1, main, body');
    const text = parse(getComputedStyle(target).color);
    const back = background(target);
    const [hi, lo] = [luminance(text), luminance(back)].sort((a, b) => b - a);
    return { ratio: (hi + 0.05) / (lo + 0.05), backgroundLuminance: luminance(back) };
  });
}

async function assertNoOverflow(page, label) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  assert.equal(overflow, false, `${label}: horizontal overflow at 1440 px`);
}

/** Keyboard: Tab until the element named `name` receives focus. */
async function tabTo(page, name, limit = 60) {
  const target = page.getByRole('button', { name, exact: true }).first();
  for (let index = 0; index < limit; index++) {
    await page.keyboard.press('Tab');
    if (await target.evaluate((element) => document.activeElement === element).catch(() => false)) return;
  }
  assert.fail(`"${name}" is not reachable by keyboard within ${limit} Tab presses`);
}

// ------------------------------------------------------------------- checks
const evidenceStage = (page, title) => page.locator('.cockpit-table__evidence-stage', { hasText: title });
const stageWord = async (page, title) => (await evidenceStage(page, title).locator('.evidence-state').first().innerText()).trim();

/**
 * Each check: scenario number/name, the scenario to activate, a `view` hash for
 * the theme pass, `expect` texts that must be visible there, and `run` for the
 * behaviour assertions (light scheme only).
 */
const checks = [
  {
    number: 1, name: 'environment_ready', scenario: 'environment_ready',
    view: '#/cockpit/diagnostics', expect: [/No required check failed or lacks an answer/, /Passed\s+16/, /Failed\s+0/],
    async run({ page, base }) {
      await open(page, base, '#/cockpit/diagnostics');
      await settle(page);
      await page.getByText('No required check failed or lacks an answer.').waitFor();
      await page.keyboard.press('Tab');
      await tabTo(page, 'Refresh');
      await page.keyboard.press('Enter');
      await page.getByText('No required check failed or lacks an answer.').waitFor();
      await open(page, base, `#/cockpit/table/${ORDERS}/proofs`);
      await page.getByText('Live evidence').waitFor();
      assert.match(await bodyText(page), /Laboratory-injected live condition/, 'the injected live condition is disclosed');
    },
  },
  {
    number: 11, name: 'read_without_delivery', scenario: 'read_without_delivery',
    view: `#/cockpit/table/${ORDERS}/proofs`, expect: [/Journal capture/, /Snowflake delivery/],
    async run({ page, base }) {
      await open(page, base, `#/cockpit/table/${ORDERS}/proofs`);
      await page.getByText('Journal capture').waitFor();
      assert.equal(await stageWord(page, 'Journal capture'), 'Observed');
      for (const stage of ['Snowflake delivery', 'Destination reconciliation']) {
        assert.equal(await stageWord(page, stage), 'Not measured', `${stage} must not be presented as established`);
      }
    },
  },
  {
    number: 12, name: 'evidence_absent', scenario: 'evidence_absent',
    view: `#/cockpit/table/${ORDERS}/proofs`, expect: [/No evidence collected/],
    async run({ page, base }) {
      await open(page, base, `#/cockpit/table/${ORDERS}/proofs`);
      await page.getByText('No evidence collected').first().waitFor();
      for (const stage of ['Journal capture', 'Snowflake delivery', 'Destination reconciliation']) {
        assert.equal(await stageWord(page, stage), 'Not measured', `${stage}: nothing is inferred from the declared state`);
      }
    },
  },
  {
    number: 13, name: 'evidence_stale', scenario: 'evidence_stale',
    view: `#/cockpit/table/${ORDERS}/proofs`, expect: [/Snowflake delivery/, /Stale/],
    async run({ page, base }) {
      await open(page, base, `#/cockpit/table/${ORDERS}/proofs`);
      await page.getByText('Snowflake delivery').waitFor();
      assert.equal(await stageWord(page, 'Journal capture'), 'Observed');
      assert.equal(await stageWord(page, 'Snowflake delivery'), 'Stale');
      assert.equal(await stageWord(page, 'Destination reconciliation'), 'Stale');
      await page.getByRole('button', { name: 'Refresh', exact: true }).click();
      await page.getByText('Snowflake delivery').waitFor();
      assert.equal(await stageWord(page, 'Snowflake delivery'), 'Stale', 'refresh does not hide staleness');
      await open(page, base, '#/cockpit/diagnostics');
      await settle(page);
      await page.getByText('delivery.snowflake').first().waitFor();
      assert.match(await bodyText(page), /Failed\s+[1-9]/, 'a stale delivery is reported as a failed check');
    },
  },
  {
    number: 14, name: 'late_old_pipeline_response', scenario: 'late_old_pipeline_response',
    view: `#/cockpit/table/${CUSTOMERS}/proofs`, expect: [/LABLIB\.CUSTOMERS/],
    async run({ page, base }) {
      await open(page, base, `#/cockpit/table/${ORDERS}/proofs`); // its evidence read is delayed 3 s by the transport
      await page.evaluate((hash) => { location.hash = hash; }, `#/cockpit/table/${CUSTOMERS}/proofs`);
      await page.getByRole('heading', { level: 1, name: 'LABLIB.CUSTOMERS' }).waitFor();
      await page.getByText('Journal capture').waitFor();
      await page.waitForTimeout(4_000); // the former pipeline's answer arrives now
      assert.equal(await page.locator('h1').innerText(), 'LABLIB.CUSTOMERS');
      const text = await bodyText(page);
      assert.match(text, /fixture for LABLIB\.CUSTOMERS/);
      assert.doesNotMatch(text, /fixture for LABLIB\.ORDERS/, 'a late answer must not replace the current table');
    },
  },
  {
    number: 15, name: 'action_confirmed_reread', scenario: 'action_confirmed_reread',
    view: '#/cockpit/confirmations', expect: [/Confirmations/],
    async run({ page, base }) {
      // Keyboard path: F9 focuses the controls, Enter opens the confirmation, Tab reaches Confirm.
      await open(page, base, `#/cockpit/table/${ORDERS}/metrics`);
      await page.getByRole('heading', { level: 1, name: 'LABLIB.ORDERS' }).waitFor();
      await page.keyboard.press('F9');
      const focused = await page.evaluate(() => document.activeElement?.textContent?.trim());
      assert.equal(focused, 'Suspend', 'F9 moves focus to the first action without triggering it');
      assert.deepEqual(await ledger(base, 'pipeline_executor'), []);
      await page.keyboard.press('Enter');
      await page.getByRole('alertdialog', { name: 'Confirm action' }).waitFor();
      assert.deepEqual(await ledger(base, 'pipeline_executor'), [], 'nothing runs before confirmation');
      await tabTo(page, 'Confirm');
      await page.keyboard.press('Enter');
      await page.getByText('Effect verified by reading again.').waitFor();
      assert.equal((await ledger(base, 'pipeline_executor')).filter((e) => e.event === 'pause').length, 1);
      const reread = await page.evaluate(async (id) => (await (await fetch(`/v2/pipelines/${id}`)).json()).declared_state, ORDERS);
      assert.equal(reread, 'paused');

      // Sensitive action: pending confirmation, approval, execution, read back.
      await open(page, base, `#/cockpit/table/${CUSTOMERS}/metrics`);
      await page.getByRole('heading', { level: 1, name: 'LABLIB.CUSTOMERS' }).waitFor();
      await page.locator('#cockpit-primary-controls summary', { hasText: 'More' }).click();
      await page.getByRole('menuitem', { name: 'Restart the initial copy' }).click();
      await page.getByRole('alertdialog', { name: 'Confirm action' }).waitFor();
      await page.getByRole('button', { name: 'Confirm', exact: true }).click();
      await page.getByText('Sensitive action: awaiting approval.').waitFor();
      assert.equal((await ledger(base, 'pipeline_executor')).filter((e) => e.event === 'restart_initial_copy').length, 0);
      await page.getByRole('button', { name: 'Approve', exact: true }).click();
      await page.getByRole('button', { name: 'Restart, now approved' }).click();
      await page.getByText('Effect verified by reading again.').waitFor();
      assert.equal((await ledger(base, 'pipeline_executor')).filter((e) => e.event === 'restart_initial_copy').length, 1);
    },
  },
  {
    number: 16, name: 'timeout_effect_unknown', scenario: 'timeout_effect_unknown', slow: true,
    async run({ page, base }) {
      // The pause is executed but its answer is withheld longer than the 30 s action timeout.
      // (The dry-run preview that precedes it is let through untouched.)
      await labApi(base, '/lab/faults', {
        method: 'PUT',
        body: JSON.stringify({ id: 'lost-pause-response', mode: 'delay_after', method: 'POST',
          path: `/v2/pipelines/${ORDERS}/actions/pause`, seconds: 33, remaining: 1, skip: 1 }), // skip: the dry-run preview comes first
      });
      await open(page, base, `#/cockpit/table/${ORDERS}/metrics`);
      await page.getByRole('heading', { level: 1, name: 'LABLIB.ORDERS' }).waitFor();
      await page.getByRole('button', { name: 'Suspend', exact: true }).click();
      await page.getByRole('button', { name: 'Confirm', exact: true }).click();
      await page.getByText('Result unknown.').waitFor({ timeout: 45_000 });
      assert.match(await bodyText(page), /nothing was retried automatically/);
      const events = (await ledger(base, 'pipeline_executor')).filter((e) => e.event === 'pause');
      assert.equal(events.length, 1, 'no second mutation was sent');
      const state = await page.evaluate(async (id) => (await (await fetch(`/v2/pipelines/${id}`)).json()).declared_state, ORDERS);
      assert.equal(state, 'paused', 'the effect did happen; only the answer was lost');
      assert.equal((await ledger(base, 'pipeline_executor')).filter((e) => e.event === 'pause').length, 1);
    },
  },
  {
    number: 19, name: 'export_canaries', scenario: 'export_canaries',
    view: '#/cockpit/diagnostics', expect: [/Diagnostic export/],
    async run({ page, base }) {
      await open(page, base, '#/cockpit/diagnostics');
      await settle(page);
      await page.getByRole('button', { name: 'Prepare diagnostic export' }).click();
      await page.getByText(/report\.json/).first().waitFor();
      const text = await bodyText(page);
      assert.match(text, /report\.json/, 'the preview lists the files before any download');
      assert.ok(await page.getByRole('button', { name: 'Download' }).isVisible(), 'download is offered only after the preview');
      const preview = text.slice(text.indexOf('Diagnostic export'));
      for (const canary of CANARIES) assert.ok(!preview.includes(canary), `canary visible in the export preview: ${canary}`);
      const html = await page.content();
      for (const canary of SECRET_CANARIES) assert.ok(!html.includes(canary), `secret canary present in the DOM: ${canary}`);
      const storage = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
      assert.equal(storage, '{}{}', 'preparing an export stores nothing in the browser');
    },
  },
  {
    number: 20, name: 'resume_after_refresh', scenario: 'resume_after_refresh',
    view: '#/cockpit/confirmations', expect: [/Confirmations/],
    async run({ page, base, context }) {
      await open(page, base, `#/cockpit/table/${CUSTOMERS}/metrics`);
      await page.getByRole('heading', { level: 1, name: 'LABLIB.CUSTOMERS' }).waitFor();
      await page.locator('#cockpit-primary-controls summary', { hasText: 'More' }).click();
      await page.getByRole('menuitem', { name: 'Restart the initial copy' }).click();
      await page.getByRole('button', { name: 'Confirm', exact: true }).click();
      await page.getByText('Sensitive action: awaiting approval.').waitFor();

      await page.reload();
      assert.match(page.url(), new RegExp(`#/cockpit/table/${CUSTOMERS}/metrics$`), 'the screen is restored from the address');
      await page.getByRole('heading', { level: 1, name: 'LABLIB.CUSTOMERS' }).waitFor();
      await open(page, base, '#/cockpit/confirmations');
      await settle(page);
      await page.getByText(CUSTOMERS).first().waitFor();
      assert.doesNotMatch(await bodyText(page), /No pending decisions/);
      // A brand-new page in the same context is still signed in and sees the same decision.
      const fresh = await context.newPage();
      await open(fresh, base, '#/cockpit/confirmations');
      await fresh.getByText(CUSTOMERS).first().waitFor();
      await fresh.close();
      assert.equal((await ledger(base, 'pipeline_executor')).filter((e) => e.event === 'restart_initial_copy').length, 0,
        'refreshing never executes the pending action');
    },
  },
];

// ------------------------------------------------------------------ runner
const results = [];

async function launchBrowser() {
  const executablePath = process.env.QUADRINGENT_BROWSER_EXECUTABLE;
  return chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : { channel: process.env.QUADRINGENT_BROWSER_CHANNEL || 'chrome' }),
  });
}

async function newSession(browser, base, colorScheme) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme, reducedMotion: 'reduce' });
  const page = await context.newPage();
  const watch = new Watch(page);
  await page.goto(new URL('/#/cockpit', base).href);
  await page.getByRole('heading', { name: 'Sign in' }).waitFor();
  assert.match(page.url(), /#\/login\?returnTo=/, 'an unauthenticated visit is sent to sign-in');
  await signIn(page, watch);
  await page.getByRole('heading', { level: 1, name: 'Cockpit' }).waitFor();
  assert.deepEqual(watch.take(), [], 'sign-in: no unexplained console or network error');
  return { context, page, watch };
}

async function shot(page, scheme, check) {
  if (!screenshotsDir) return;
  await page.screenshot({ path: join(screenshotsDir, `${String(check.number).padStart(2, '0')}-${check.name}-${scheme}.png`) });
}

async function record(label, body) {
  const started = Date.now();
  try {
    await body();
    results.push({ label, ok: true, ms: Date.now() - started });
    console.log(`PASS ${label} (${Date.now() - started} ms)`);
  } catch (error) {
    results.push({ label, ok: false, ms: Date.now() - started, error: error.message });
    console.log(`FAIL ${label}: ${error.message.slice(0, 900)}`);
  }
}

async function runMainChecks(browser, base) {
  const selected = checks.filter((check) => (!only || only.includes(check.number)) && !(skipSlow && check.slow));
  const light = await newSession(browser, base, 'light');
  const dark = await newSession(browser, base, 'dark');
  try {
    for (const check of selected) {
      await activate(base, check.scenario);
      await record(`scenario ${String(check.number).padStart(2, '0')} ${check.name}`, async () => {
        await check.run({ ...light, base });
        await assertNoOverflow(light.page, check.name);
        await shot(light.page, 'light', check);
        assert.deepEqual(light.watch.take(), [], `${check.name}: no unexplained console or network error`);
      });
      if (!check.view) continue;
      // Same scenario, dark scheme: the same states stay visible and legible.
      await record(`scenario ${String(check.number).padStart(2, '0')} ${check.name} (dark scheme)`, async () => {
        const { page, watch } = dark;
        assert.equal(await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches), true);
        await open(page, base, check.view);
        await settle(page);
        await page.waitForTimeout(400);
        const text = await bodyText(page);
        for (const pattern of check.expect) assert.match(text, pattern);
        const measured = await contrast(page);
        assert.ok(measured.ratio >= 4.5, `text contrast ${measured.ratio.toFixed(2)} < 4.5 in dark scheme`);
        assert.ok(measured.backgroundLuminance < 0.2, 'the dark scheme renders a dark background');
        await assertNoOverflow(page, `${check.name} dark`);
        await shot(page, 'dark', check);
        assert.deepEqual(watch.take(), [], `${check.name} dark: no unexplained console or network error`);
      });
    }
  } finally {
    await light.context.close();
    await dark.context.close();
  }
}

/** Scenario 10: the real process is stopped, the port closes, the cockpit says so. */
async function runControlPlaneDown(browser) {
  for (const scheme of ['light', 'dark']) {
    const label = `scenario 10 control_plane_down (${scheme} scheme)`;
    await record(label, async () => {
      const lab = await startLab({ scenario: 'baseline', allowShutdown: true });
      let session;
      try {
        session = await newSession(browser, lab.url, scheme);
        const { page, watch } = session;
        await page.getByRole('heading', { level: 1, name: 'Cockpit' }).waitFor();
        await page.getByRole('table').waitFor();
        await labApi(lab.url, '/lab/shutdown', { method: 'POST' });
        await new Promise((done) => lab.child.once('exit', done));
        watch.expectOutage = true;
        await page.keyboard.press('F5'); // refresh by keyboard while the port is closed
        await page.getByRole('heading', { name: 'Service unavailable' }).waitFor({ timeout: 15_000 });
        assert.equal(await page.getByRole('alert').first().isVisible(), true);
        const text = await bodyText(page);
        assert.doesNotMatch(text, /Laboratory IBM i/, 'no stale connection list is presented as current');
        const measured = await contrast(page);
        assert.ok(measured.ratio >= 4.5, `contrast ${measured.ratio.toFixed(2)}`);
        await assertNoOverflow(page, 'control plane down');
        if (screenshotsDir) await page.screenshot({ path: join(screenshotsDir, `10-control_plane_down-${scheme}.png`) });
        assert.deepEqual(watch.take(), [], 'no unexplained error besides the refused connections');
      } finally {
        await session?.context.close();
        await stopLab(lab);
      }
    });
  }
}

// -------------------------------------------------------------------- main
let mainLab = null;
let browser = null;
try {
  browser = await launchBrowser();
  const wantMain = !only || checks.some((check) => only.includes(check.number) && !(skipSlow && check.slow));
  if (wantMain) {
    mainLab = attachUrl ? { url: attachUrl } : await startLab({ scenario: 'baseline' });
    await runMainChecks(browser, mainLab.url);
  }
  if (!only || only.includes(10)) await runControlPlaneDown(browser);
} finally {
  await browser?.close();
  if (mainLab?.child) await stopLab(mainLab);
}

const failed = results.filter((result) => !result.ok);
console.log(JSON.stringify({
  result: failed.length === 0 ? 'PASS' : 'FAIL',
  passed: results.length - failed.length,
  failed: failed.length,
  scope: 'Desktop 1440x900, light and dark colour schemes, keyboard, real frontend against the real v2 control plane (laboratory boundaries)',
  limits: 'All data is synthetic; live evidence is a laboratory-injected condition; no IBM i, Snowflake or cloud was contacted',
}));
process.exit(failed.length === 0 ? 0 : 1);
