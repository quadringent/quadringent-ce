import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { DEMO_CSP, assertSafeDemoOutDir, demoProvenance, verifyDemoOutput } from './demo-artifact.mjs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createDemoServer } from './serve-demo.mjs';

function demoTree({ csp = DEMO_CSP, banner = 'Synthetic demo. Every value is generated locally.', synthetic = true, guard = 'Synthetic demo: no network request is allowed.' } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'quadringent-demo-'));
  mkdirSync(join(root, 'assets'));
  writeFileSync(join(root, 'index.html'),
    `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${csp}"></head>`
    + '<body><div id="root"></div><script type="module" src="./assets/demo.js"></script></body></html>');
  writeFileSync(join(root, 'assets', 'demo.js'), `console.log(${JSON.stringify(banner)}, ${JSON.stringify(guard)});`);
  writeFileSync(join(root, 'PROVENANCE.json'), JSON.stringify({ ...demoProvenance({
    version: '0.3.5', commit: 'a'.repeat(40), builtAtUtc: '2026-10-12T18:00:00Z',
  }), synthetic }));
  return root;
}

test('provenance names version, commit and UTC build time and declares no connection', () => {
  const provenance = demoProvenance({ version: '0.3.5', commit: 'b'.repeat(40), builtAtUtc: '2026-10-12T18:00:00Z' });
  assert.equal(provenance.synthetic, true);
  assert.equal(provenance.connections, 'none');
  assert.equal(provenance.version, '0.3.5');
  assert.equal(provenance.sourceCommit, 'b'.repeat(40));
  assert.equal(provenance.builtAtUtc, '2026-10-12T18:00:00Z');
  assert.match(provenance.notice, /not a release qualification/);
  assert.throws(() => demoProvenance({ version: '0.3.5', commit: 'short', builtAtUtc: '2026-10-12T18:00:00Z' }));
  assert.throws(() => demoProvenance({ version: '0.3.5', commit: 'b'.repeat(40), builtAtUtc: '2026-10-12 18:00' }));
});

test('a complete demo tree passes verification', () => {
  assert.doesNotThrow(() => verifyDemoOutput(demoTree()));
});

test('verification rejects a demo that could reach the network', () => {
  assert.throws(() => verifyDemoOutput(demoTree({ csp: "default-src 'self'" })), /connect-src 'none'/);
});

test('verification rejects a demo without its network guard', () => {
  assert.throws(() => verifyDemoOutput(demoTree({ guard: 'none' })), /network guard/);
});

test('verification rejects a demo without its synthetic banner or provenance flag', () => {
  assert.throws(() => verifyDemoOutput(demoTree({ banner: 'Cockpit' })), /Synthetic demo/);
  assert.throws(() => verifyDemoOutput(demoTree({ synthetic: false })), /synthetic/);
});

let server;
let port;
const root = demoTree();

before(async () => {
  server = createDemoServer({ root });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  port = server.address().port;
});

after(() => new Promise((resolve) => server.close(resolve)));

function fetchLocal(path, method = 'GET') {
  return new Promise((resolve, reject) => {
    const call = request({ host: '127.0.0.1', port, path, method }, (response) => {
      let body = '';
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body }));
    });
    call.on('error', reject);
    call.end();
  });
}

test('the local server serves the demo with a no-network content security policy', async () => {
  const page = await fetchLocal('/');
  assert.equal(page.status, 200);
  assert.match(page.headers['content-security-policy'], /connect-src 'none'/);
  assert.match(page.headers['content-type'], /text\/html/);
  const script = await fetchLocal('/assets/demo.js');
  assert.equal(script.status, 200);
  assert.match(script.headers['content-type'], /javascript/);
});

test('the local server refuses traversal, unknown files and writes', async () => {
  assert.equal((await fetchLocal('/../package.json')).status, 404);
  assert.equal((await fetchLocal('/%2e%2e/package.json')).status, 404);
  assert.equal((await fetchLocal('/missing.js')).status, 404);
  assert.equal((await fetchLocal('/', 'POST')).status, 405);
});

test('the local server only binds to loopback', () => {
  assert.throws(() => createDemoServer({ root, host: '0.0.0.0' }), /loopback/);
});

test('the demo build only writes into build/, an empty folder or an earlier demo output', () => {
  const repo = mkdtempSync(join(tmpdir(), 'quadringent-repo-'));
  mkdirSync(join(repo, 'build'));
  assert.doesNotThrow(() => assertSafeDemoOutDir(join(repo, 'build', 'quadringent-demo-0.3.5'), repo));
  const empty = mkdtempSync(join(tmpdir(), 'quadringent-empty-'));
  assert.doesNotThrow(() => assertSafeDemoOutDir(empty, repo));
  assert.doesNotThrow(() => assertSafeDemoOutDir(join(empty, 'new-folder'), repo));
  const earlier = demoTree();
  assert.doesNotThrow(() => assertSafeDemoOutDir(earlier, repo));
  const victim = mkdtempSync(join(tmpdir(), 'quadringent-victim-'));
  writeFileSync(join(victim, 'important.txt'), 'keep');
  assert.throws(() => assertSafeDemoOutDir(victim, repo), /not empty/);
  for (const forbidden of [repo, join(repo, 'build'), join(repo, 'ui'), join(repo, 'ui', 'dist'), '/']) {
    assert.throws(() => assertSafeDemoOutDir(forbidden, repo));
  }
});

test('verify-build rejects a product build that contains demonstration code', () => {
  const dist = mkdtempSync(join(tmpdir(), 'quadringent-dist-'));
  mkdirSync(join(dist, 'assets'));
  writeFileSync(join(dist, 'assets', 'index-abc.js'), 'const banner = "Synthetic demo";');
  const script = fileURLToPath(new URL('./verify-build.mjs', import.meta.url));
  const result = spawnSync(process.execPath, [script, dist], { encoding: 'utf8' });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /fixture ou fallback de démonstration/);
});
