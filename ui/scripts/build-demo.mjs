#!/usr/bin/env node
// Builds the synthetic demo as a separate artifact: its own entry
// (src/demo/index.html), Vite mode `demo`, and a page that cannot open a
// network connection. The product build (`npm run build`) and its
// verify-build.mjs gate are untouched.
//
//   node ui/scripts/build-demo.mjs [--out <directory>] [--no-archive] [--serve]
//
// --serve then serves the result on http://127.0.0.1:8870/#/cockpit.
// Output: <repo>/build/quadringent-demo-<version>/ and, unless --no-archive,
// <repo>/build/quadringent-demo-<version>.tar.gz with a .sha256 file.
import { copyFileSync, createReadStream, existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { build } from 'vite';
import { DEMO_CSP, assertSafeDemoOutDir, demoProvenance, verifyDemoOutput } from './demo-artifact.mjs';
import { createDemoServer } from './serve-demo.mjs';

const uiDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoDir = resolve(uiDir, '..');
const args = process.argv.slice(2);
const outIndex = args.indexOf('--out');

const version = /productVersion = '([^']+)'/.exec(readFileSync(join(uiDir, 'src', 'version.ts'), 'utf8'))?.[1];
const commit = git(['rev-parse', 'HEAD']);
const sourceDirty = git(['status', '--porcelain', '--untracked-files=no']) !== '';
const epoch = process.env.SOURCE_DATE_EPOCH ? Number(process.env.SOURCE_DATE_EPOCH) * 1000 : Date.now();
const builtAtUtc = new Date(epoch).toISOString().replace(/\.\d{3}Z$/, 'Z');
const provenance = demoProvenance({ version, commit, builtAtUtc, sourceDirty });

const name = `quadringent-demo-${version}`;
const outDir = outIndex >= 0 ? resolve(process.cwd(), args[outIndex + 1]) : join(repoDir, 'build', name);
assertSafeDemoOutDir(outDir, repoDir);

await build({
  configFile: false,
  root: join(uiDir, 'src', 'demo'),
  envDir: uiDir,
  base: './',
  mode: 'demo',
  publicDir: false,
  logLevel: 'warn',
  plugins: [react(), {
    name: 'quadringent-demo-csp',
    transformIndexHtml: (html) => html.replace('__DEMO_CSP__', DEMO_CSP),
  }],
  resolve: { preserveSymlinks: true },
  define: {
    __QUADRINGENT_DEMO_PROVENANCE__: JSON.stringify({ version, sourceCommit: commit, sourceDirty, builtAtUtc }),
  },
  build: { outDir, emptyOutDir: true, target: 'es2022', sourcemap: false },
});

writeFileSync(join(outDir, 'PROVENANCE.json'), `${JSON.stringify(provenance, null, 2)}\n`);
copyFileSync(join(uiDir, 'scripts', 'serve-demo.mjs'), join(outDir, 'serve-demo.mjs'));
writeFileSync(join(outDir, 'README.txt'), [
  `Quadringent Community ${version} - synthetic demo`,
  '',
  'Synthetic data generated in the browser. No IBM i, Snowflake or cloud connection:',
  "the page blocks network requests (connect-src 'none'). Not a release qualification.",
  '',
  'Requires Node.js 18 or later. From this directory:',
  '  node serve-demo.mjs',
  'then open http://127.0.0.1:8870/#/cockpit',
  '',
  `Source commit: ${commit}${sourceDirty ? ' (with uncommitted changes)' : ''}`,
  `Built (UTC): ${builtAtUtc}`,
  '',
].join('\n'));
verifyDemoOutput(outDir);
console.log(`Synthetic demo built in ${outDir}`);

if (!args.includes('--no-archive')) {
  const archive = `${outDir}.tar.gz`;
  if (existsSync(archive)) rmSync(archive);
  const tar = spawnSync('tar', ['-czf', archive, '-C', dirname(outDir), basename(outDir)], { stdio: 'inherit' });
  if (tar.status !== 0) throw new Error('tar failed to create the demo archive');
  const digest = await sha256(archive);
  writeFileSync(`${archive}.sha256`, `${digest}  ${basename(archive)}\n`);
  console.log(`Archive ${archive}\nSHA-256 ${digest}`);
}

if (args.includes('--serve')) {
  createDemoServer({ root: outDir }).listen(8870, '127.0.0.1', () => {
    console.log('Quadringent synthetic demo: http://127.0.0.1:8870/#/cockpit');
    console.log('No IBM i, Snowflake or cloud connection. Press Ctrl+C to stop.');
  });
}

function git(gitArgs) {
  const result = spawnSync('git', gitArgs, { cwd: repoDir, encoding: 'utf8' });
  if (result.status !== 0) throw new Error(`git ${gitArgs.join(' ')} failed: ${result.stderr}`);
  return result.stdout.trim();
}

function sha256(file) {
  return new Promise((resolveDigest, reject) => {
    const hash = createHash('sha256');
    createReadStream(file).on('data', (chunk) => hash.update(chunk)).on('end', () => resolveDigest(hash.digest('hex'))).on('error', reject);
  });
}
