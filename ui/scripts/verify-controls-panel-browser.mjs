// Régression navigateur isolée : node ui/scripts/verify-controls-panel-browser.mjs
// Après installation des dépendances UI ; Playwright via QUADRINGENT_PLAYWRIGHT_MODULE
// et navigateur local optionnel via QUADRINGENT_BROWSER_EXECUTABLE, comme les autres sondes.
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';

const { chromium } = await import(process.env.QUADRINGENT_PLAYWRIGHT_MODULE ?? 'playwright');
const html = `<!doctype html><html lang="fr"><body><div id="root"></div>
<script type="module">
import React from 'react';
import { createRoot } from 'react-dom/client';
import { ControlsPanel } from '/src/components/ControlsPanel.tsx';
import '/src/styles/product-shell.css';
window.calls = [];
createRoot(document.getElementById('root')).render(React.createElement(ControlsPanel, {
 level: 'table', targetLabel: 'DEMO.TABLE', availableActions: ['restart_initial_copy'],
 dryRun: async action => { window.calls.push(['dryRun',action]); return {}; },
 execute: async action => { window.calls.push(['execute',action]); return {pendingConfirmationId: null}; },
 verify: async () => { window.calls.push(['verify']); }
}));
</script></body></html>`;
const server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { host: '127.0.0.1', port: 0 }, logLevel: 'silent',
 plugins: [{name:'controls-interaction-page', configureServer(s) {
  s.middlewares.use('/__controls_interaction', async (_req,res) => {
   res.setHeader('Content-Type','text/html'); res.end(await s.transformIndexHtml('/__controls_interaction',html));
  });
 }}] });
let browser;
try {
 await server.listen();
 browser = await chromium.launch({headless:true,
  ...(process.env.QUADRINGENT_BROWSER_EXECUTABLE ? {executablePath:process.env.QUADRINGENT_BROWSER_EXECUTABLE} : {})});
 const page = await browser.newPage({viewport:{width:1440,height:1000}});
 await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/__controls_interaction`);
 await page.getByText('Plus',{exact:true}).click();
 assert.equal(await page.locator('details').evaluate(e=>e.open),true);
 await page.getByRole('menuitem',{name:'Relancer la copie initiale',exact:true}).click();
 await page.getByRole('alertdialog').waitFor();
 assert.equal(await page.locator('details').evaluate(e=>e.open),false,'Le menu doit se fermer avant la confirmation');
 await page.getByRole('button',{name:'Confirmer',exact:true}).click();
 await page.waitForFunction(()=>window.calls.some(c=>c[0]==='verify'));
 assert.deepEqual(await page.evaluate(()=>window.calls),[
  ['dryRun','restart_initial_copy'],['execute','restart_initial_copy'],['verify']]);
 console.log('PASS: Plus → aperçu → confirmation accessible → exécution unique → vérification');
} finally {await browser?.close();await server.close();}
