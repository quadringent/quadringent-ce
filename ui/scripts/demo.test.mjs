import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, utimesSync, writeFileSync } from 'node:fs';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { DEMO_CSP, assertSafeDemoOutDir, demoProvenance, verifyDemoOutput } from './demo-artifact.mjs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createDemoServer } from './serve-demo.mjs';
import * as artifacts from './demo-artifact.mjs';
import { gunzipSync } from 'node:zlib';

const temporaryRoot = realpathSync(tmpdir());

function demoTree({ csp = DEMO_CSP, banner = 'Synthetic demo. Every value is generated locally.', synthetic = true, guard = 'Synthetic demo: no network request is allowed.' } = {}) {
  const root = mkdtempSync(join(temporaryRoot, 'quadringent-demo-'));
  mkdirSync(join(root, 'assets'));
  writeFileSync(join(root, 'index.html'),
    `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${csp}"></head>`
    + '<body><div id="root"></div><script type="module" src="./assets/demo.js"></script></body></html>');
  writeFileSync(join(root, 'assets', 'demo.js'), `console.log(${JSON.stringify(banner)}, ${JSON.stringify(guard)});`);
  writeFileSync(join(root, 'PROVENANCE.json'), JSON.stringify({ ...demoProvenance({
    version: '0.3.5', commit: 'a'.repeat(40), builtAtUtc: '2026-10-12T18:00:00Z',
  }), synthetic }));
  writeFileSync(join(root, 'LICENSE'), 'Project license');
  writeFileSync(join(root, 'NOTICE'), 'Project notice');
  writeFileSync(join(root, 'third-party-notices.txt'), 'react@19\n@fontsource-variable/ibm-plex-sans@5\n@fontsource/ibm-plex-mono@5\n');
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

test('verification requires project and dependency notices', () => {
  for (const file of ['LICENSE', 'NOTICE', 'third-party-notices.txt']) {
    const directory = demoTree();
    rmSync(join(directory, file));
    assert.throws(() => verifyDemoOutput(directory), /license|notice|ENOENT/i);
  }
  const directory = demoTree();
  writeFileSync(join(directory, 'third-party-notices.txt'), 'react@19 only');
  assert.throws(() => verifyDemoOutput(directory), /font|notice/i);
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
  const repo = mkdtempSync(join(temporaryRoot, 'quadringent-repo-'));
  mkdirSync(join(repo, 'build'));
  assert.doesNotThrow(() => assertSafeDemoOutDir(join(repo, 'build', 'quadringent-demo-0.3.5'), repo));
  const empty = mkdtempSync(join(temporaryRoot, 'quadringent-empty-'));
  assert.doesNotThrow(() => assertSafeDemoOutDir(empty, repo));
  assert.doesNotThrow(() => assertSafeDemoOutDir(join(empty, 'new-folder'), repo));
  const earlier = demoTree();
  assert.doesNotThrow(() => assertSafeDemoOutDir(earlier, repo));
  const victim = mkdtempSync(join(temporaryRoot, 'quadringent-victim-'));
  writeFileSync(join(victim, 'important.txt'), 'keep');
  assert.throws(() => assertSafeDemoOutDir(victim, repo), /not empty/);
  for (const forbidden of [repo, join(repo, 'build'), join(repo, 'ui'), join(repo, 'ui', 'dist'), '/']) {
    assert.throws(() => assertSafeDemoOutDir(forbidden, repo));
  }
});

test('demo output rejects symlinks and linked ancestors without touching their targets', () => {
  const repo = mkdtempSync(join(temporaryRoot, 'quadringent-link-repo-'));
  mkdirSync(join(repo, 'build'));
  const victim = mkdtempSync(join(temporaryRoot, 'quadringent-link-victim-'));
  const sentinel = join(victim, 'important.txt');
  writeFileSync(sentinel, 'keep');
  const linked = join(repo, 'build', 'linked');
  symlinkSync(victim, linked, 'dir');
  assert.throws(() => assertSafeDemoOutDir(linked, repo), /symbolic link/i);
  assert.throws(() => assertSafeDemoOutDir(join(linked, 'new'), repo), /symbolic link/i);
  const broken = join(repo, 'build', 'broken');
  symlinkSync(join(victim, 'missing'), broken, 'dir');
  assert.throws(() => assertSafeDemoOutDir(broken, repo), /symbolic link/i);
  assert.equal(readFileSync(sentinel, 'utf8'), 'keep');
});

test('demo archives use neutral metadata, retain notices and extract with tar', () => {
  assert.equal(typeof artifacts.writeDemoArchive, 'function');
  const directory = demoTree();
  writeFileSync(join(directory, '._private'), 'private metadata');
  writeFileSync(join(directory, '.DS_Store'), 'private metadata');
  const archive = `${directory}.tar.gz`;
  const mtime = 1_760_000_000;
  artifacts.writeDemoArchive(directory, archive, mtime);
  const bytes = readFileSync(archive);
  utimesSync(join(directory, 'index.html'), new Date(), new Date());
  artifacts.writeDemoArchive(directory, archive, mtime);
  assert.deepEqual(readFileSync(archive), bytes);
  const tar = gunzipSync(bytes);
  const names = [];
  for (let offset = 0; tar[offset];) {
    const header = tar.subarray(offset, offset + 512);
    const field = (start, length) => header.subarray(start, start + length).toString().replace(/\0.*$/s, '');
    const name = field(0, 100), prefix = field(345, 155);
    names.push(prefix ? `${prefix}/${name}` : name);
    assert.equal(parseInt(field(108, 8), 8), 0);
    assert.equal(parseInt(field(116, 8), 8), 0);
    assert.equal(field(265, 32), '');
    assert.equal(field(297, 32), '');
    assert.equal(parseInt(field(136, 12), 8), mtime);
    assert.ok(['0', '5'].includes(field(156, 1)));
    offset += 512 + Math.ceil(parseInt(field(124, 12), 8) / 512) * 512;
  }
  for (const name of ['LICENSE', 'NOTICE', 'third-party-notices.txt']) {
    assert.ok(names.some((entry) => entry.endsWith(`/${name}`)));
  }
  assert.ok(!names.some((entry) => entry.includes('._') || entry.endsWith('.DS_Store')));
  const extracted = mkdtempSync(join(temporaryRoot, 'quadringent-demo-extract-'));
  const result = spawnSync('tar', ['-xzf', archive, '-C', extracted], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(readFileSync(join(extracted, names[0], 'LICENSE'), 'utf8'), 'Project license');
});

test('archive output and payload links are refused without changing their targets', () => {
  const directory = demoTree();
  const victim = mkdtempSync(join(temporaryRoot, 'quadringent-archive-victim-'));
  const sentinel = join(victim, 'important.txt');
  writeFileSync(sentinel, 'keep');
  const archive = `${directory}.tar.gz`;
  symlinkSync(sentinel, archive);
  assert.throws(() => artifacts.writeDemoArchive(directory, archive, 1_760_000_000), /symbolic link/i);
  symlinkSync(sentinel, `${archive}.sha256`);
  assert.throws(() => artifacts.assertNoSymbolicLinks(`${archive}.sha256`), /symbolic link/i);
  rmSync(archive);
  symlinkSync(sentinel, join(directory, 'assets', 'linked.txt'));
  assert.throws(() => artifacts.writeDemoArchive(directory, archive, 1_760_000_000), /Unsupported.*entry/);
  assert.equal(readFileSync(sentinel, 'utf8'), 'keep');
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
