import { existsSync, lstatSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { basename, dirname, join, parse, relative, resolve, sep } from 'node:path';
import { gzipSync } from 'node:zlib';
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
  for (const name of ['LICENSE', 'NOTICE', 'third-party-notices.txt']) {
    if (!readFileSync(join(directory, name), 'utf8').trim()) throw new Error(`Empty license/notice: ${name}`);
  }
  const notices = readFileSync(join(directory, 'third-party-notices.txt'), 'utf8');
  for (const dependency of ['react@', '@fontsource-variable/ibm-plex-sans@', '@fontsource/ibm-plex-mono@']) {
    if (!notices.includes(dependency)) throw new Error(`Dependency notice missing: ${dependency}`);
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
  assertNoSymbolicLinks(target);
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

export function assertNoSymbolicLinks(path) {
  for (let current = resolve(path);; current = dirname(current)) {
    try {
      if (lstatSync(current).isSymbolicLink()) throw new Error(`Demo output cannot contain a symbolic link: ${current}`);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    if (current === dirname(current)) return;
  }
}

/** USTAR headers use only payload bytes and public metadata; platform tar
 *  implementations otherwise include local owners and macOS xattrs. */
export function writeDemoArchive(directory, archive, mtime) {
  if (!Number.isSafeInteger(mtime) || mtime < 0) throw new Error('Archive time must be a non-negative epoch');
  assertNoSymbolicLinks(directory);
  assertNoSymbolicLinks(archive);
  const blocks = [];
  function append(path, name) {
    const metadata = lstatSync(path);
    if (!metadata.isDirectory() && !metadata.isFile()) throw new Error(`Unsupported demo archive entry: ${name}`);
    const content = metadata.isFile() ? readFileSync(path) : Buffer.alloc(0);
    blocks.push(tarHeader(name, content.length, metadata.isDirectory(), mtime));
    if (content.length) blocks.push(content, Buffer.alloc((512 - content.length % 512) % 512));
    if (metadata.isDirectory()) {
      for (const child of readdirSync(path).sort()) {
        if (child === '.DS_Store' || child.startsWith('._')) continue;
        append(join(path, child), `${name}/${child}`);
      }
    }
  }
  append(directory, basename(resolve(directory)));
  blocks.push(Buffer.alloc(1024));
  const contents = gzipSync(Buffer.concat(blocks));
  if (existsSync(archive)) rmSync(archive);
  writeFileSync(archive, contents, { flag: 'wx' });
}

function tarHeader(path, size, directory, mtime) {
  const header = Buffer.alloc(512);
  let name = path, prefix = '';
  if (Buffer.byteLength(name) > 100) {
    let split = path.lastIndexOf('/');
    while (split >= 0 && (Buffer.byteLength(path.slice(0, split)) > 155 || Buffer.byteLength(path.slice(split + 1)) > 100)) {
      split = path.lastIndexOf('/', split - 1);
    }
    if (split < 0) throw new Error(`Demo archive path is too long: ${path}`);
    prefix = path.slice(0, split); name = path.slice(split + 1);
  }
  header.write(name, 0, 100);
  header.write(prefix, 345, 155);
  const octal = (value, offset, length) => {
    const digits = value.toString(8);
    if (digits.length >= length) throw new Error('Demo archive metadata exceeds USTAR limits');
    header.write(digits.padStart(length - 1, '0') + '\0', offset, length);
  };
  octal(directory ? 0o755 : 0o644, 100, 8);
  octal(0, 108, 8); octal(0, 116, 8);
  octal(size, 124, 12); octal(mtime, 136, 12);
  header.fill(32, 148, 156);
  header.write(directory ? '5' : '0', 156, 1);
  header.write('ustar\0', 257, 6); header.write('00', 263, 2);
  const checksum = header.reduce((sum, byte) => sum + byte, 0);
  header.write(checksum.toString(8).padStart(6, '0') + '\0 ', 148, 8);
  return header;
}
