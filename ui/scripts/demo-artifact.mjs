import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, parse, relative, resolve, sep } from 'node:path';
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
  if (!scripts.some((file) => readFileSync(file, 'utf8').includes('Every value is generated locally'))) {
    throw new Error('The demo bundle must render the Synthetic demo banner');
  }
  if (!scripts.some((file) => readFileSync(file, 'utf8').includes('Synthetic demo: no network request is allowed.'))) {
    throw new Error('The demo bundle must install the network guard');
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

/** The build empties its output folder, so it only accepts a folder under
 *  <repo>/build/, an empty or missing folder, or an earlier demo output. */
export function assertSafeDemoOutDir(outDir, repoDir) {
  const target = resolve(outDir);
  const repo = resolve(repoDir);
  const build = join(repo, 'build');
  if (target === parse(target).root) throw new Error('The demo output cannot be a filesystem root');
  const protectedDirs = [repo, build, join(repo, 'ui'), join(repo, 'ui', 'dist'), join(repo, 'ui', 'src'), process.env.HOME]
    .filter(Boolean).map((dir) => resolve(dir));
  if (protectedDirs.includes(target)) throw new Error(`The demo output cannot be ${target}`);
  const inside = (parent) => target.startsWith(parent + sep);
  if (inside(repo) && !inside(build)) throw new Error('Inside the repository, the demo output must be under build/');
  if (inside(build) || !existsSync(target)) return;
  const entries = readdirSync(target);
  if (entries.length === 0) return;
  try {
    const provenance = JSON.parse(readFileSync(join(target, 'PROVENANCE.json'), 'utf8'));
    if (provenance.kind === 'synthetic-demo' && provenance.synthetic === true) return;
  } catch {
    // Not an earlier demo output.
  }
  throw new Error(`${relative(process.cwd(), target) || target} is not empty and is not a demo output`);
}
