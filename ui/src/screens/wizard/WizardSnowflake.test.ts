import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import { ControlPlaneV2Client, type DestinationRecord } from '../../data/controlPlaneV2Client.ts';

let vite: ViteDevServer;
let WizardSnowflake: ComponentType<{ existingDestination?: DestinationRecord | null; client: ControlPlaneV2Client; onBack: () => void; onContinue: (id: string) => void }>;
let SnowflakeDestinationReceipt: ComponentType<{
  destination: DestinationRecord | null;
  copied: boolean;
  onCopy: () => void;
  onContinue: (id: string) => void;
  onVerify: () => void;
  verification?: 'idle' | 'checking' | 'verified' | 'failed';
  verificationDetail?: string | null;
}>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  SnowflakeDestinationReceipt = (await vite.ssrLoadModule('/src/screens/wizard/WizardSnowflake.tsx')).SnowflakeDestinationReceipt;
  WizardSnowflake = (await vite.ssrLoadModule('/src/screens/wizard/WizardSnowflake.tsx')).WizardSnowflake;
});

after(async () => { await vite.close(); });

const privateKey = 'CLE_SYNTHETIQUE_UNIQUE';
const replayMessage = "The key was provided when the destination was created. It cannot be downloaded again. Use your retained copy.";

async function creationReply(withKey: boolean): Promise<DestinationRecord> {
  const client = new ControlPlaneV2Client({
    fetchFn: async () => new Response(JSON.stringify({
      before: null,
      after: {
        id: 'dst_test', snowflake_account: 'test-account', verification_state: 'declared_not_verified',
        setup_script: 'CREATE ROLE EXEMPLE;', ...(withKey ? { private_key_pem: privateKey } : {}),
      },
      verify: { method: 'GET', path: '/v2/destinations/dst_test' }, dry_run: null,
    }), { status: 201, headers: { 'Content-Type': 'application/json' } }),
  });
  return client.createDestination({ accountIdentifier: 'test-account' });
}

function renderReceipt(destination: DestinationRecord | null, verification: 'idle' | 'checking' | 'verified' | 'failed' = 'idle', verificationDetail: string | null = null): string {
  return renderToStaticMarkup(createElement(SnowflakeDestinationReceipt, {
    destination, verification, verificationDetail, copied: false, onCopy() {}, onContinue() {}, onVerify() {},
  }));
}

test('l’assistant propose la base et le schéma facultatif avec le contrat historique visible', () => {
  const markup = renderToStaticMarkup(createElement(WizardSnowflake, {
    client: new ControlPlaneV2Client(), onBack: () => {}, onContinue: () => {},
  }));
  assert.match(markup, /id="wizard-snowflake-database"[^>]*value="QUADRINGENT"/);
  assert.match(markup, /id="wizard-snowflake-schema"/);
  assert.match(markup, /history goes to RAW and the mirror to CURATED/);
});

test('la première réponse conserve le téléchargement de la clé et la consigne initiale', async () => {
  const destination = await creationReply(true);
  const markup = renderReceipt(destination);
  assert.equal(destination.privateKeyPem, privateKey);
  assert.match(markup, /href="data:application\/x-pem-file;charset=utf-8,CLE_SYNTHETIQUE_UNIQUE"/);
  assert.match(markup, /download="quadringent-snowflake-private-key.pem"/);
  assert.match(markup, /retain a copy of the private key/);
  assert.ok(!markup.includes(replayMessage));
  assert.match(markup, />Verify access<\/button>/);
  assert.match(markup, /class="action-button action-button--primary" disabled="">Continue<\/button>/);
});

test('le rejeu sans clé garde le script et attend la vérification, sans téléchargement trompeur', async () => {
  const destination = await creationReply(false);
  const markup = renderReceipt(destination);
  assert.equal(destination.privateKeyPem, null);
  assert.ok(markup.includes(replayMessage));
  assert.match(markup, /CREATE ROLE EXEMPLE;/);
  assert.match(markup, /download="quadringent-snowflake-setup.sql"/);
  assert.doesNotMatch(markup, /quadringent-snowflake-private-key.pem|data:application\/x-pem-file|download the private key/);
  assert.match(markup, /class="action-button action-button--primary" disabled="">Continue<\/button>/);
});

test('un ancien état vérifié ne suffit pas à ouvrir la suite de l’assistant', async () => {
  const destination = { ...await creationReply(false), verificationState: 'verified' as const };
  for (const state of ['idle', 'checking', 'failed'] as const) {
    assert.match(renderReceipt(destination, state), /class="action-button action-button--primary" disabled="">Continue<\/button>/);
  }
});

test('la suite nécessite la sonde confirmée et son état persisté vérifié', async () => {
  const destination = await creationReply(false);
  assert.match(renderReceipt(destination, 'verified'), /class="action-button action-button--primary" disabled="">Continue<\/button>/);
  const markup = renderReceipt({ ...destination, verificationState: 'verified' }, 'verified');
  assert.match(markup, /class="action-button action-button--primary">Continue<\/button>/);
  assert.match(markup, /Snowflake access verified/);
});

test('pendant la sonde, les deux actions sont désactivées et l’effet annoncé', async () => {
  const markup = renderReceipt(await creationReply(true), 'checking');
  assert.match(markup, /disabled="">Verification in progress…<\/button>/);
  assert.match(markup, /class="action-button action-button--primary" disabled="">Continue<\/button>/);
  assert.match(markup, /creates and removes a test table/);
  assert.match(markup, /may resume the warehouse/);
});

test('un échec rend le diagnostic visible et permet une nouvelle sonde', async () => {
  const markup = renderReceipt(await creationReply(false), 'failed', 'Droit CREATE TABLE absent.');
  assert.match(markup, /role="alert">Snowflake access is unverified/);
  assert.match(markup, /Droit CREATE TABLE absent\./);
  assert.match(markup, /class="action-button">Verify access<\/button>/);
  assert.match(markup, /class="action-button action-button--primary" disabled="">Continue<\/button>/);
});

test('avant création, aucun secret ni script et Continuer reste désactivé', () => {
  const markup = renderReceipt(null);
  assert.doesNotMatch(markup, /download=|CREATE ROLE|key has been delivered/);
  assert.match(markup, /class="action-button action-button--primary" disabled="">Continue<\/button>/);
});

test('resumed destination shows its exact scope and no new key, and requires a fresh probe', async () => {
 const destination={...await creationReply(false),accountIdentifier:'synthetic-org-account',destinationDatabase:'DB_A',destinationSchema:'SITE_A',verificationState:'verified' as const};
 const markup=renderToStaticMarkup(createElement(WizardSnowflake,{client:new ControlPlaneV2Client(),existingDestination:destination,onBack(){},onContinue(){}}));
 assert.match(markup,/value="synthetic-org-account"/);
 assert.match(markup,/value="SITE_A"/);
 assert.ok(markup.includes(replayMessage));
 assert.match(markup,/class="action-button action-button--primary" disabled="">Continue/);
 assert.doesNotMatch(markup,/quadringent-snowflake-private-key.pem/);
});
