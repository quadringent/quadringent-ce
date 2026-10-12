import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';
import { createServer as createSocketServer } from 'node:net';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const { chromium } = await import(process.env.QUADRINGENT_PLAYWRIGHT_MODULE || 'playwright');
const html = `<!doctype html><html lang="en"><body><div id="root"></div><script type="module">
import React from 'react';
import { createRoot } from 'react-dom/client';
import { CockpitTableView } from '/src/screens/cockpit/CockpitTable.tsx';
import '/src/styles/product-tokens.css';
import '/src/styles/base.css';
import '/src/styles/foundation.css';
import '/src/styles/cockpit.css';
const noop = () => {};
createRoot(document.getElementById('root')).render(React.createElement(CockpitTableView, {
  pipelineId: 'synthetic-keyboard', tab: 'proofs', window: '1h',
  state: { status: 'ready', data: {
    pipeline: { id: 'synthetic-keyboard', declaredState: 'not_started' },
    metrics: { window: '1h', points: [] }, logs: [], costToday: null,
  } },
  onWindowChange: noop, onLevelChange: noop, correlateIncident: false,
  onCorrelateIncidentChange: noop, onRefresh: noop,
}));
</script></body></html>`;
const socket = createSocketServer();
await new Promise((resolve, reject) => {
  socket.once('error', reject);
  socket.listen(0, '127.0.0.1', resolve);
});
const port = socket.address().port;
await new Promise(resolve => socket.close(resolve));
const cacheDir = await mkdtemp(join(tmpdir(), 'quadringent-keyboard-'));
const server = await createServer({
  root: fileURLToPath(new URL('../', import.meta.url)),
  cacheDir,
  server: { host: '127.0.0.1', port, strictPort: true }, logLevel: 'silent',
  plugins: [{ name: 'cockpit-keyboard-test', configureServer(vite) {
    vite.middlewares.use('/__keyboard', async (_request, response) => {
      response.setHeader('Content-Type', 'text/html');
      response.end(await vite.transformIndexHtml('/__keyboard', html));
    });
  } }],
});
let browser;
let page;
const errors = [];
try {
  await server.listen();
  browser = await chromium.launch({ headless: true,
    ...(process.env.QUADRINGENT_BROWSER_EXECUTABLE ? { executablePath: process.env.QUADRINGENT_BROWSER_EXECUTABLE } : {}),
  });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/__keyboard`);
  const evidence = page.getByRole('tab', { name: 'Evidence', exact: true });
  const metrics = page.getByRole('tab', { name: 'Metrics', exact: true });
  await evidence.focus();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('Tab');
  assert.equal(await page.getByRole('button', { name: 'Suspend', exact: true }).evaluate(button => button === document.activeElement), true, 'Tab leaves the tablist');
  await page.keyboard.press('Shift+Tab');
  assert.equal(await metrics.evaluate(tab => tab === document.activeElement), true, 'Shift+Tab returns to the focused tab');
  assert.equal(await metrics.getAttribute('tabindex'), '0');
  assert.equal(await evidence.getAttribute('tabindex'), '-1');
  assert.equal(await evidence.getAttribute('aria-selected'), 'true');
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.getByRole('button', { name: 'Refresh', exact: true }).evaluate(button => button === document.activeElement), true, 'Shift+Tab leaves the tablist backwards');
  await page.keyboard.press('Tab');
  assert.equal(await metrics.evaluate(tab => tab === document.activeElement), true, 'Tab re-enters on the focused tab');
  await evidence.focus();
  for (const label of ['Costs', 'Logs', 'Latest rows', 'Metrics', 'Evidence']) {
    await page.keyboard.press('ArrowLeft');
    assert.equal(await page.getByRole('tab', { name: label, exact: true }).evaluate(tab => tab === document.activeElement), true, label);
    assert.equal(await evidence.getAttribute('aria-selected'), 'true');
    assert.deepEqual(await page.getByRole('tab').evaluateAll(tabs => tabs.filter(tab => tab.tabIndex === 0).map(tab => tab.textContent)), [label]);
  }
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.getByRole('tab', { name: 'Metrics', exact: true }).evaluate(tab => tab === document.activeElement), true);
  await page.keyboard.press('Enter');
  assert.equal(await page.evaluate(() => location.hash), '#/cockpit/table/synthetic-keyboard/metrics');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'PASS', synthetic: true, port, viewport: { width: 1440, height: 1000 },
    checks: ['arrows', 'roving tabindex', 'Tab/Shift+Tab', 'wrap', 'manual Enter activation'], errors }));
} catch (error) {
  console.error(JSON.stringify({ errors, body: page ? (await page.locator('body').innerText()).slice(0, 500) : null }));
  throw error;
} finally {
  await browser?.close();
  await server.close();
  await rm(cacheDir, { recursive: true, force: true });
}
