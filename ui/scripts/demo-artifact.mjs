import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { DEMO_CSP } from './serve-demo.mjs';

export { DEMO_CSP };

const COMMIT = /^[0-9a-f]{40}$/;
const UTC_SECONDS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

/** Provenance written next to the demo build and shown in its banner. */
export function demoProvenance({ version, commit, builtAtUtc, sourceDirty = false }) {
  if (typeof version !== 'string' || !/^\d+\.\d+\.\d+/.test(version)) throw new Error(`Invalid version: ${version}`);
  if (!COMMIT.test(commit ?? '')) throw new Error('A full 40-character source commit is required');
  if (!UTC_SECONDS.test(builtAtUtc ?? '')) throw new Error('Build time must be UTC, as YYYY-MM-DDTHH:MM:SSZ');
  return {
    product: 'Quadringent Community',
    kind: 'synthetic-demo',
    synthetic: true,
    connections: 'none',
    version,
    sourceCommit: commit,
    sourceDirty: Boolean(sourceDirty),
    builtAtUtc,
    notice: 'Synthetic data generated in the browser. No IBM i, Snowflake or cloud connection; '
      + 'this demo is not a release qualification.',
  };
}

/** Fails unless the build cannot reach the network and identifies itself
 *  as synthetic, in both its page policy and its provenance. */
export function verifyDemoOutput(directory) {
  const page = readFileSync(join(directory, 'index.html'), 'utf8');
  const policy = /http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(page)?.[1] ?? '';
  if (!policy.split(';').map((item) => item.trim()).includes("connect-src 'none'")) {
    throw new Error("The demo page must declare connect-src 'none'");
  }
  const scripts = walk(directory).filter((file) => file.endsWith('.js'));
  if (!scripts.some((file) => readFileSync(file, 'utf8').includes('Synthetic demo'))) {
    throw new Error('The demo bundle must render the Synthetic demo banner');
  }
  const provenance = JSON.parse(readFileSync(join(directory, 'PROVENANCE.json'), 'utf8'));
  if (provenance.synthetic !== true || provenance.connections !== 'none') {
    throw new Error('PROVENANCE.json must declare synthetic: true and connections: none');
  }
  return provenance;
}

function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}
