#!/usr/bin/env node
// Static loopback server for the Quadringent synthetic demo. No dependency:
// it is copied into the portable demo archive next to index.html.
import { createServer } from 'node:http';
import { readFileSync, realpathSync, statSync } from 'node:fs';
import { extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Page policy, also written as a <meta> tag in the demo index.html. */
export const DEMO_CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self' data:",
  "connect-src 'none'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join('; ');

/** frame-ancestors is only honoured as an HTTP header, not in <meta>. */
const DEMO_CSP_HEADER = `${DEMO_CSP}; frame-ancestors 'none'`;

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.txt': 'text/plain; charset=utf-8',
};

const LOOPBACK = new Set(['127.0.0.1', '::1', 'localhost']);

export function createDemoServer({ root, host = '127.0.0.1' }) {
  if (!LOOPBACK.has(host)) throw new Error(`The synthetic demo only listens on loopback, not ${host}`);
  const base = realpathSync(resolve(root));
  return createServer((request, response) => {
    const headers = { 'Content-Security-Policy': DEMO_CSP_HEADER, 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' };
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      response.writeHead(405, { ...headers, Allow: 'GET, HEAD' }).end();
      return;
    }
    const file = resolveFile(base, request.url ?? '/');
    if (!file) {
      response.writeHead(404, { ...headers, 'Content-Type': 'text/plain; charset=utf-8' }).end('Not found');
      return;
    }
    const body = readFileSync(file);
    response.writeHead(200, { ...headers, 'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream' });
    response.end(request.method === 'HEAD' ? undefined : body);
  });
}

function resolveFile(base, url) {
  let path;
  try {
    path = decodeURIComponent(new URL(url, 'http://demo.invalid').pathname);
  } catch {
    return null;
  }
  if (path.includes('\0') || path.split('/').includes('..')) return null;
  const candidate = resolve(join(base, path.endsWith('/') ? `${path}index.html` : path));
  try {
    const real = realpathSync(candidate);
    if (real !== base && !real.startsWith(base + sep)) return null;
    return statSync(real).isFile() ? real : null;
  } catch {
    return null;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const option = (name, fallback) => {
    const index = args.indexOf(name);
    return index >= 0 ? args[index + 1] : fallback;
  };
  const here = fileURLToPath(new URL('.', import.meta.url));
  const root = resolve(option('--root', here));
  const port = Number(option('--port', '8870'));
  const host = option('--host', '127.0.0.1');
  const server = createDemoServer({ root, host });
  server.listen(port, host, () => {
    console.log(`Quadringent synthetic demo: http://${host}:${port}/#/cockpit`);
    console.log('No IBM i, Snowflake or cloud connection. Press Ctrl+C to stop.');
  });
}
