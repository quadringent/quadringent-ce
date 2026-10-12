/**
 * Mode démo du cockpit v2 — sert des réponses fixes, sans backend, pour le
 * développement local et les tests d'écran. Même principe que
 * `wizardDemo.ts` : `createCockpitDemoFetch` renvoie une fonction
 * compatible avec l'option `fetchFn` de `ControlPlaneV2Client`.
 *
 * Jeu de données : deux connexions (Ventes, Stocks), huit tables au total,
 * un incident (`pl_sales_order_lines` en `attention`), une table en pause
 * (`pl_inventory_movements`), une copie initiale en cours
 * (`pl_inventory_items`), le reste en direct. Séries de métriques 1 h/24 h
 * synthétiques mais déterministes (pas de `Math.random()` : un test qui lit
 * deux fois la même fenêtre doit voir les mêmes points). Coûts couvrant les
 * trois provenances (mesuré/estimé/absent).
 */

import type { PipelineDeclaredState } from '../controlPlaneV2Client.ts';

export const DEMO_SOURCE_SALES = 'src_demo_sales';
export const DEMO_SOURCE_INVENTORY = 'src_demo_inventory';
export const DEMO_DESTINATION_SALES = 'dst_demo_sales';
export const DEMO_DESTINATION_INVENTORY = 'dst_demo_inventory';

interface DemoPipeline {
  readonly id: string;
  readonly sourceId: string;
  readonly name: string;
  declaredState: PipelineDeclaredState;
}

const DEMO_PIPELINES: readonly DemoPipeline[] = [
  { id: 'pl_sales_customers', sourceId: DEMO_SOURCE_SALES, name: "CUSTOMERS", declaredState: 'live' },
  { id: 'pl_sales_orders', sourceId: DEMO_SOURCE_SALES, name: "ORDERS", declaredState: 'live' },
  { id: 'pl_sales_order_lines', sourceId: DEMO_SOURCE_SALES, name: "ORDER_LINES", declaredState: 'attention' },
  { id: 'pl_sales_archive', sourceId: DEMO_SOURCE_SALES, name: "ARCHIVE_90D", declaredState: 'live' },
  { id: 'pl_inventory_items', sourceId: DEMO_SOURCE_INVENTORY, name: "ITEMS", declaredState: 'copying' },
  { id: 'pl_inventory_movements', sourceId: DEMO_SOURCE_INVENTORY, name: "MOVEMENTS", declaredState: 'paused' },
  { id: 'pl_inventory_warehouses', sourceId: DEMO_SOURCE_INVENTORY, name: "WAREHOUSES", declaredState: 'live' },
  { id: 'pl_inventory_alerts', sourceId: DEMO_SOURCE_INVENTORY, name: "THRESHOLD_ALERTS", declaredState: 'live' },
];

/** L'incident de démonstration : corrèle les journaux de `pl_sales_order_lines`. */
const DEMO_INCIDENT_ID = 'inc_demo_order_lines_lag';

let confirmationSequence = 0;

export function createCockpitDemoFetch(): (input: string, init?: RequestInit) => Promise<Response> {
  const pipelines = DEMO_PIPELINES.map((pipeline) => ({ ...pipeline }));
  const confirmations = new Map<string, { readonly actionRef: string; readonly resourceId: string; state: 'pending' | 'approved' | 'rejected' | 'used' }>();

  return async (input, init) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const [path, search] = input.replace(/^.*\/v2/, '').split('?');
    const query = new URLSearchParams(search ?? '');
    const body = init?.body ? JSON.parse(init.body as string) as Record<string, unknown> : {};

    if (method === 'GET' && path === '/sources') {
      return json(200, {
        items: [
          { id: DEMO_SOURCE_SALES, display_name: "Sales", ibmi_host: 'as400-sales.demo.local' },
          { id: DEMO_SOURCE_INVENTORY, display_name: "Inventory", ibmi_host: 'as400-inventory.demo.local' },
        ],
        next_cursor: null,
      });
    }

    if (method === 'GET' && path === '/destinations') {
      return json(200, {
        items: [
          { id: DEMO_DESTINATION_SALES, account_identifier: 'demo-sales-xy1', verification_state: 'verified', sql_script: '' },
          { id: DEMO_DESTINATION_INVENTORY, account_identifier: 'demo-inventory-xy2', verification_state: 'verified', sql_script: '' },
        ],
        next_cursor: null,
      });
    }

    if (method === 'GET' && path === '/pipelines') {
      return json(200, { items: pipelines.map(pipelineListDict), next_cursor: null });
    }

    const pipelineMatch = /^\/pipelines\/([^/]+)$/.exec(path);
    if (method === 'GET' && pipelineMatch) {
      const pipeline = pipelines.find((item) => item.id === pipelineMatch[1]);
      if (!pipeline) return notFound(path);
      return json(200, pipelineDict(pipeline));
    }

    const evidenceMatch = /^\/pipelines\/([^/]+)\/evidence$/.exec(path);
    if (method === 'GET' && evidenceMatch) {
      const pipeline = pipelines.find((item) => item.id === evidenceMatch[1]);
      if (!pipeline) return notFound(path);
      return json(200, demoEvidence(pipeline));
    }

    const actionMatch = /^\/pipelines\/([^/]+)\/actions\/([^/]+)$/.exec(path);
    if (method === 'POST' && actionMatch) {
      const [, pipelineId, action] = actionMatch;
      const pipeline = pipelines.find((item) => item.id === pipelineId);
      if (!pipeline) return notFound(path);
      const dryRun = body.dry_run === true;
      const sensitive = action === 'remove' || action === 'restart_initial_copy' || action === 'replay';

      if (dryRun) {
        return json(200, envelope(null, null, `/pipelines/${pipelineId}`, { action, effect: demoActionEffect(action) }));
      }

      if (sensitive) {
        const token = typeof body.confirmation_token === 'string' ? body.confirmation_token : null;
        const confirmation = token ? confirmations.get(token) : null;
        if (!confirmation || confirmation.state !== 'approved' || confirmation.resourceId !== pipelineId || confirmation.actionRef !== `pipeline.${action}`) {
          confirmationSequence += 1;
          const confirmationId = `cf_demo_${confirmationSequence}`;
          confirmations.set(confirmationId, { actionRef: `pipeline.${action}`, resourceId: pipelineId, state: 'pending' });
          return json(409, {
            error: {
              code: 'pending_confirmation_required',
              message: `confirmation required (id=${confirmationId}) — see /v2/confirmations/${confirmationId}`,
              next_action: "Approve the confirmation.",
              retryable: false,
            },
          });
        }
      }

      const before = pipelineDict(pipeline);
      pipeline.declaredState = nextDeclaredState(action as PipelineActionKind, pipeline.declaredState);
      if (sensitive) confirmations.get(body.confirmation_token as string)!.state = 'used';
      return json(200, envelope(before, pipelineDict(pipeline), `/pipelines/${pipelineId}`, null));
    }

    const levelActionMatch = /^\/(sources|destinations)\/([^/]+)\/actions\/(pause|resume)$/.exec(path);
    if (method === 'POST' && levelActionMatch) {
      const [, kind, id, action] = levelActionMatch;
      const sourceId = kind === 'sources' ? id : sourceIdForDestination(id);
      const affected = pipelines.filter((pipeline) => pipeline.sourceId === sourceId);
      if (body.dry_run === true) {
        return json(200, envelope(null, null, `/${kind}/${id}`, { action, effect: demoActionEffect(action) }));
      }
      for (const pipeline of affected) pipeline.declaredState = action === 'pause' ? 'paused' : 'live';
      return json(200, envelope(null, { affected: affected.map((item) => item.id) }, `/${kind}/${id}`, null));
    }

    const fleetActionMatch = /^\/actions\/(pause_all|resume_all)$/.exec(path);
    if (method === 'POST' && fleetActionMatch) {
      if (body.dry_run === true) {
        const action = fleetActionMatch[1];
        return json(200, envelope(null, null, '/pipelines', { action, effect: demoActionEffect(action) }));
      }
      for (const pipeline of pipelines) pipeline.declaredState = fleetActionMatch[1] === 'pause_all' ? 'paused' : 'live';
      return json(200, envelope(null, { affected: pipelines.map((item) => item.id) }, '/pipelines', null));
    }

    const metricsMatch = /^\/pipelines\/([^/]+)\/metrics$/.exec(path);
    if (method === 'GET' && metricsMatch) {
      const window = query.get('window') === '24h' ? '24h' : '1h';
      const paused = pipelines.find((item) => item.id === metricsMatch[1])?.declaredState === 'paused';
      return json(200, { points: demoMetricsPoints(metricsMatch[1], window, paused) });
    }

    const logsMatch = /^\/pipelines\/([^/]+)\/logs$/.exec(path);
    if (method === 'GET' && logsMatch) {
      return json(200, { items: demoLogs(logsMatch[1], query.get('level'), query.get('correlate_incident') === 'true') });
    }

    if (method === 'GET' && path === '/costs') {
      return json(200, demoCost(query.get('scope'), query.get('id')));
    }

    if (method === 'GET' && path === '/confirmations') {
      const stateFilter = query.get('state') ?? 'pending';
      const items = [...confirmations.entries()]
        .filter(([, record]) => stateFilter === 'all' || record.state === stateFilter)
        .map(([id, record]) => confirmationDict(id, record));
      return json(200, { items, next_cursor: null });
    }

    const confirmationMatch = /^\/confirmations\/([^/]+)$/.exec(path);
    if (method === 'GET' && confirmationMatch) {
      const record = confirmations.get(confirmationMatch[1]);
      return record ? json(200, confirmationDict(confirmationMatch[1], record)) : notFound(path);
    }

    const approveMatch = /^\/confirmations\/([^/]+)\/approve$/.exec(path);
    if (method === 'POST' && approveMatch) {
      const record = confirmations.get(approveMatch[1]);
      if (!record) return notFound(path);
      record.state = 'approved';
      return json(200, envelope(null, confirmationDict(approveMatch[1], record), `/confirmations/${approveMatch[1]}`, null));
    }

    const rejectMatch = /^\/confirmations\/([^/]+)\/reject$/.exec(path);
    if (method === 'POST' && rejectMatch) {
      const record = confirmations.get(rejectMatch[1]);
      if (!record) return notFound(path);
      record.state = 'rejected';
      return json(200, envelope(null, confirmationDict(rejectMatch[1], record), `/confirmations/${rejectMatch[1]}`, null));
    }

    if (method === 'GET' && path === '/audit') {
      return json(200, { items: [], next_cursor: null });
    }

    return notFound(path);

    function json(status: number, payload: unknown): Response {
      return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
    }
  };
}

type PipelineActionKind = 'pause' | 'resume' | 'remove' | 'restart_initial_copy' | 'replay';

function nextDeclaredState(action: PipelineActionKind, current: PipelineDeclaredState): PipelineDeclaredState {
  switch (action) {
    case 'pause': return 'paused';
    case 'resume': return 'live';
    case 'remove': return 'stopped';
    case 'restart_initial_copy': return 'copying';
    case 'replay': return current;
  }
}

function demoActionEffect(action: string): string {
  switch (action) {
    case 'pause': case 'pause_all': return "Suspends reading; the current position is retained.";
    case 'resume': case 'resume_all': return "Resumes reading from the last recorded position.";
    case 'remove': return "Permanently removes this item from tracking.";
    case 'restart_initial_copy': return "Restarts the initial copy from the beginning.";
    case 'replay': return "Replays events from the specified range.";
    default: return "Effect not described.";
  }
}

function pipelineDict(pipeline: DemoPipeline): Record<string, unknown> {
  return { id: pipeline.id, declared_state: pipeline.declaredState };
}

/** `GET /v2/pipelines` (liste) porte, en plus de `pipelineDict`, l'id de
 *  table/destination et les figures observées — voir
 *  `PipelineListRecord.to_dict` côté service. Une table en pause n'a plus
 *  de lecture active : retard/débit/lignes destination/dernière arrivée
 *  restent `null`, avec leur raison, jamais une valeur figée maquillée en
 *  mesure fraîche. Les lignes source restent connues (comptage IBM i,
 *  indépendant de l'état du pipeline). */
function pipelineListDict(pipeline: DemoPipeline): Record<string, unknown> {
  const paused = pipeline.declaredState === 'paused';
  const baseLag = (pipeline.name.length * 7) % 90;
  const baseThroughput = (pipeline.name.length * 3) % 40 + 5;
  const rowsSource = 10_000 + pipeline.name.length * 2_500;
  // La copie initiale en cours n'a pas encore rattrapé la source ; un
  // incident (« attention ») laisse un petit écart visible entre les deux
  // comptes — jamais un chiffre inventé, dérivé du même id déterministe.
  const rowsDestination = pipeline.declaredState === 'copying'
    ? Math.round(rowsSource * 0.62)
    : pipeline.declaredState === 'attention'
      ? rowsSource - 340
      : rowsSource;
  const absentReasons: Record<string, string> = {};
  if (paused) {
    absentReasons.lag_seconds = "Table paused — no reading in progress.";
    absentReasons.throughput_rows_per_second = "Table paused — no reading in progress.";
    absentReasons.rows_destination = "Table paused — last synchronization not refreshed.";
    absentReasons.last_arrival_at = "Table paused — no arrivals since the pause.";
  }
  return {
    ...pipelineDict(pipeline),
    table_id: `tbl_${pipeline.id}`,
    source_id: pipeline.sourceId,
    destination_id: destinationIdForSource(pipeline.sourceId),
    lag_seconds: paused ? null : baseLag,
    throughput_rows_per_second: paused ? null : baseThroughput,
    rows_source: rowsSource,
    rows_destination: paused ? null : rowsDestination,
    last_arrival_at: paused ? null : '2026-09-23T09:00:00Z',
    absent_reasons: absentReasons,
  };
}

function destinationIdForSource(sourceId: string): string {
  return sourceId === DEMO_SOURCE_SALES ? DEMO_DESTINATION_SALES : DEMO_DESTINATION_INVENTORY;
}

function confirmationDict(id: string, record: { readonly actionRef: string; readonly resourceId: string; readonly state: string }): Record<string, unknown> {
  return {
    id,
    action_ref: record.actionRef,
    resource_type: 'pipeline',
    resource_id: record.resourceId,
    reason: `Sensitive action “${record.actionRef}” on ${record.resourceId} (demonstration).`,
    risk_estimate: null,
    requested_by_kind: 'human',
    requested_by_id: 'op_demo',
    expires_at: null,
    state: record.state,
    approved_by_kind: record.state === 'approved' ? 'human' : null,
    approved_by_id: record.state === 'approved' ? 'op_demo' : null,
    approved_at: record.state === 'approved' ? '2026-09-23T09:30:00Z' : null,
    created_at: '2026-09-23T09:00:00Z',
    available: {
      approve: record.state === 'pending',
      reject: record.state === 'pending',
      execute: record.state === 'approved' && ['pipeline.remove', 'pipeline.restart_initial_copy'].includes(record.actionRef),
    },
  };
}

function sourceIdForDestination(destinationId: string): string {
  return destinationId === DEMO_DESTINATION_SALES ? DEMO_SOURCE_SALES : DEMO_SOURCE_INVENTORY;
}

/** Série déterministe : un point toutes les 5 min sur 1 h (12 points), toutes
 *  les heures sur 24 h (24 points). Le retard oscille légèrement autour d'une
 *  valeur propre à chaque pipeline (dérivée de son id) — jamais aléatoire. */
function demoMetricsPoints(pipelineId: string, window: '1h' | '24h', paused: boolean): readonly Record<string, unknown>[] {
  const pipeline = DEMO_PIPELINES.find((item) => item.id === pipelineId);
  const baseLag = pipeline ? (pipeline.name.length * 7) % 90 : 30;
  const baseThroughput = pipeline ? (pipeline.name.length * 3) % 40 + 5 : 10;
  const count = window === '1h' ? 12 : 24;
  const stepSeconds = window === '1h' ? 300 : 3600;
  return Array.from({ length: count }, (_, index) => {
    const atSeconds = index * stepSeconds;
    const wobble = Math.sin(index / 2) * 5;
    return {
      at: new Date(Date.UTC(2026, 8, 23, 9, 0, 0) + atSeconds * 1000).toISOString(),
      lag_seconds: paused ? null : Math.max(0, Math.round(baseLag + wobble)),
      throughput_rows_per_second: paused ? null : Math.max(0, Math.round(baseThroughput + wobble)),
    };
  });
}

function demoLogs(pipelineId: string, level: string | null, correlateIncident: boolean): readonly Record<string, unknown>[] {
  const isIncidentPipeline = pipelineId === 'pl_sales_order_lines';
  const entries: Record<string, unknown>[] = [
    { at: '2026-09-23T08:55:00Z', level: 'info', message: "Journal reading resumed from the last checkpoint.", incident_id: null },
    { at: '2026-09-23T09:00:00Z', level: 'info', message: "Delivery window closed and confirmed in Snowflake.", incident_id: null },
  ];
  if (isIncidentPipeline) {
    entries.push(
      { at: '2026-09-23T09:10:00Z', level: 'warning', message: "Increasing lag detected in journal reading.", incident_id: DEMO_INCIDENT_ID },
      { at: '2026-09-23T09:12:00Z', level: 'error', message: "Snowflake destination connection rejected (credentials).", incident_id: DEMO_INCIDENT_ID },
    );
  }
  const byLevel = level ? entries.filter((entry) => entry.level === level) : entries;
  return correlateIncident ? byLevel.filter((entry) => entry.incident_id !== null) : byLevel;
}

function demoCost(scope: string | null, id: string | null): Record<string, unknown> {
  // Coûts mesurés au niveau connexion, estimés ou absents au niveau table,
  // selon l'id — donne aux trois provenances une table à montrer côté écran.
  if (scope === 'connection') {
    const measured = id === DEMO_SOURCE_SALES;
    return measured
      ? { window: '24h', status: 'measured', amount: 4.82, currency: 'USD', basis: 'warehouse_credits', collected_at: '2026-09-23T09:00:00Z' }
      : { window: '24h', status: 'estimated', amount: 2.10, currency: 'USD', basis: 'average_throughput_estimate', collected_at: '2026-09-23T09:00:00Z' };
  }
  if (id === 'pl_inventory_movements') {
    // La table en pause n'a aucun coût mesurable depuis sa mise en pause.
    return { window: '24h', status: 'absent', amount: null, currency: null, basis: null, collected_at: null };
  }
  return { window: '24h', status: 'estimated', amount: 0.34, currency: 'USD', basis: 'average_throughput_estimate', collected_at: '2026-09-23T09:00:00Z' };
}

/** Per-table evidence: each stage keeps its own state. Reconciliation is
 *  never claimed, and a table without collected evidence says so. */
function demoEvidence(pipeline: DemoPipeline): Record<string, unknown> {
  const absent = { state: 'absent', observed_at: null, event_count: null,
    lag_seconds: null, duplicate_count: null, detail: 'Not measured in this demonstration.' };
  const reading = { ...absent, state: 'observed', observed_at: '2026-09-23T09:00:00Z',
    event_count: 120, detail: 'Synthetic capture events; no IBM i system was queried.' };
  const delivery = { ...absent, state: 'observed', observed_at: '2026-09-23T09:00:00Z',
    event_count: 118, lag_seconds: 6.2, detail: 'Synthetic delivered batch; no Snowflake system was queried.' };
  const base = { pipeline_id: pipeline.id, provenance: 'demo_fixture', evidence_kind: 'simulation',
    collected_at: '2026-09-23T09:00:00Z', reading, delivery, reconciliation: absent };
  if (pipeline.id === 'pl_sales_archive') {
    return { ...base, evidence_kind: 'absent', collected_at: null, reading: absent, delivery: absent };
  }
  switch (pipeline.declaredState) {
    case 'attention':
      return { ...base, delivery: { ...delivery, state: 'stale', observed_at: '2026-09-23T08:40:00Z',
        event_count: 96, detail: 'Last synthetic delivery predates the incident; newer events are not confirmed.' } };
    case 'copying':
      return { ...base, delivery: { ...absent, detail: 'Initial copy in progress; no delivery window confirmed yet.' } };
    case 'paused':
      return { ...base,
        reading: { ...reading, state: 'stale', observed_at: '2026-09-23T07:30:00Z', detail: 'Reading paused; last synthetic capture before the pause.' },
        delivery: { ...delivery, state: 'stale', observed_at: '2026-09-23T07:30:00Z', detail: 'No delivery since the pause.' } };
    default:
      return base;
  }
}

function envelope(before: unknown, after: unknown, path: string, dryRun: unknown): Record<string, unknown> {
  return { before, after, verify: { method: 'GET', path: `/v2${path}` }, dry_run: dryRun };
}

function notFound(path: string): Response {
  return new Response(JSON.stringify({
    error: { code: 'not_found', message: `Unknown demonstration route: ${path}`, next_action: "Check the client call.", retryable: false },
  }), { status: 404, headers: { 'Content-Type': 'application/json' } });
}

/** Résout l'id de source (convention `pl_<source>_<table>`) et le nom d'une
 *  table de démonstration — utilisé par `domain/cockpit.ts::groupTablesBySource`. */
export function demoSourceIdForPipeline(pipelineId: string): string | null {
  return DEMO_PIPELINES.find((item) => item.id === pipelineId)?.sourceId ?? null;
}

export function demoNameForPipeline(pipelineId: string): string {
  return DEMO_PIPELINES.find((item) => item.id === pipelineId)?.name ?? pipelineId;
}
