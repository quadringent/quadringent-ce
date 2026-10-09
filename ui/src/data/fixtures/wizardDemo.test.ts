import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlPlaneV2Client } from '../controlPlaneV2Client.ts';
import { createWizardDemoFetch, DEMO_SOURCE_ID, DEMO_DESTINATION_ID } from './wizardDemo.ts';

test('the demo fetch lets the whole wizard flow run against the real client, with no network', async () => {
  const client = new ControlPlaneV2Client({ fetchFn: createWizardDemoFetch() });

  const created = await client.createSource({ displayName: 'IBM i — demo', host: 'as400.demo.local', account: 'QDTDEMO', password: 'x' });
  assert.equal(created.id, DEMO_SOURCE_ID);

  const sources = await client.listSources();
  assert.equal(sources.length, 1);
  assert.equal(sources[0]!.id, DEMO_SOURCE_ID);

  const test1 = await client.testSource(DEMO_SOURCE_ID);
  // Sans sonde câblée, la démo reproduit la forme « non disponible » du
  // serveur réel (`SourcesService.test` sans `probe`) — jamais un faux vert.
  assert.equal(test1.kind, 'unavailable');

  const destination = await client.createDestination({ accountIdentifier: 'demo-xy12345' });
  assert.equal(destination.id, DEMO_DESTINATION_ID);
  assert.match(destination.sqlScript, /CREATE ROLE/);
  assert.ok(destination.privateKeyPem);

  const tables = await client.listTables(DEMO_SOURCE_ID);
  assert.ok(tables.length >= 3);
  const noKeyTable = tables.find((table) => table.readiness === 'no_key');
  assert.ok(noKeyTable, "la démo doit fournir to moins une table prête to journal mais sans clé");

  const patched = await client.patchTableKey(noKeyTable!.id, { keyStrategy: 'rrn', acknowledgeRrn: true });
  assert.equal(patched.keyStrategy, 'rrn');
  assert.equal(patched.readiness, 'ready');

  await client.startTablePipeline(noKeyTable!.id, { dryRun: false });

  const user = await client.activateAdmin('tok_demo', 'mot-de-passe-suffisant');
  assert.equal(user.activated, true);
});


test('la destination démo se reprend par lecture sans redonner la clé ni simuler une sonde réussie', async () => {
  const client = new ControlPlaneV2Client({ fetchFn: createWizardDemoFetch() });
  assert.deepEqual(await client.listDestinations(), []);
  const created = await client.createDestination({ accountIdentifier: 'demo-selected', destinationDatabase: 'DEMO_DB', destinationSchema: 'DEMO_RAW' });
  const [listed] = await client.listDestinations();
  assert.equal(listed.id, created.id);
  assert.equal(listed.accountIdentifier, 'demo-selected');
  assert.equal(listed.destinationDatabase, 'DEMO_DB');
  assert.equal(listed.destinationSchema, 'DEMO_RAW');
  assert.equal(listed.privateKeyPem, null);
  assert.equal((await client.getDestination(created.id)).privateKeyPem, null);
  assert.equal(await client.getDestinationSetupScript(created.id), created.sqlScript);
  await assert.rejects(client.verifyDestination(created.id), (error: any) => error.status === 503 && error.code === 'capability_unavailable');
  assert.equal((await client.getDestination(created.id)).verificationState, 'declared_not_verified');
});
