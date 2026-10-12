import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { DemoApp } from './DemoApp.tsx';
import { installDemoNetworkGuard } from '../data/demoNetworkGuard.ts';
import '../styles/product-tokens.css';
import '../styles/base.css';
import '../styles/brand.css';
import '../styles/product-shell.css';
import '../styles/foundation.css';
import '../styles/wizard.css';
import '../styles/responsive.css';
import '../styles/commercial.css';
import '../styles/synthetic-demo.css';

installDemoNetworkGuard(window as unknown as Parameters<typeof installDemoNetworkGuard>[0]);

if (!window.location.hash || window.location.hash === '#' || window.location.hash === '#/') {
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#/cockpit`);
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <DemoApp provenance={__QUADRINGENT_DEMO_PROVENANCE__} />
  </StrictMode>,
);
