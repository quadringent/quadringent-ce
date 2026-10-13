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
import { controlActionCopy } from '../../domain/operator.ts';
import { sha256Hex } from './demoSha256.ts';

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
        return json(200, envelope(null, null, `/pipelines/${pipelineId}`, {
          action,
          effect: demoActionEffect(action),
          would_transition: { from: pipeline.declaredState, to: nextDeclaredState(action as PipelineActionKind, pipeline.declaredState) },
        }));
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
      return json(200, envelope(null, { pipelines: applyScopeAction(affected, action) }, `/${kind}/${id}`, null));
    }

    const fleetActionMatch = /^\/actions\/(pause_all|resume_all)$/.exec(path);
    if (method === 'POST' && fleetActionMatch) {
      if (body.dry_run === true) {
        const action = fleetActionMatch[1];
        return json(200, envelope(null, null, '/pipelines', { action, effect: demoActionEffect(action) }));
      }
      return json(200, envelope(null, { pipelines: applyScopeAction(pipelines, fleetActionMatch[1] === 'pause_all' ? 'pause' : 'resume') }, '/pipelines', null));
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

    if (method === 'GET' && path === '/diagnostics/report') {
      const pipelineId = query.get('pipeline_id');
      if (pipelineId !== null && !pipelines.some((item) => item.id === pipelineId)) return notFound(path);
      return json(200, demoDiagnostics(pipelines, pipelineId));
    }

    if (method === 'GET' && path === '/diagnostics/report/export') {
      return json(200, demoDiagnosticsExport(demoDiagnostics(pipelines, null)));
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

/** Mirrors the real scope answer: `pipelines.applied[]` lists each table changed. */
function applyScopeAction(affected: DemoPipeline[], action: string): Record<string, unknown> {
  const applied: Record<string, unknown>[] = [];
  for (const pipeline of affected) {
    const before = pipelineDict(pipeline);
    pipeline.declaredState = action === 'pause' ? 'paused' : 'live';
    applied.push({ pipeline_id: pipeline.id, before, after: pipelineDict(pipeline) });
  }
  return { applied, skipped: [] };
}

function demoActionEffect(action: string): string {
  switch (action) {
    case 'pause': case 'pause_all': return controlActionCopy('pause').effect;
    case 'resume': case 'resume_all': return controlActionCopy('resume').effect;
    case 'remove': return "Permanently removes this item from tracking.";
    case 'restart_initial_copy': return "Restarts the initial copy from the beginning.";
    case 'replay': return "Replays events from the specified range.";
    default: return "Effect not described.";
  }
}

function pipelineDict(pipeline: DemoPipeline): Record<string, unknown> {
  return { id: pipeline.id, declared_state: pipeline.declaredState };
}

// Table suspension removes current delivery measurements. It does not establish
// whether the shared journal reader stopped; source counts remain independent.
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
    absentReasons.lag_seconds = 'Table delivery suspended — lag is not measured.';
    absentReasons.throughput_rows_per_second = 'Table delivery suspended — throughput is not measured.';
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
  // Generated amounts are synthetic at every scope, never measured costs.
  if (scope === 'connection') {
    return { window: '24h', status: 'synthetic', amount: id === DEMO_SOURCE_SALES ? 4.82 : 2.10,
      currency: 'USD', basis: null, collected_at: null };
  }
  if (id === 'pl_inventory_movements') {
    // A paused table has no cost observation.
    return { window: '24h', status: 'absent', amount: null, currency: null, basis: null, collected_at: null };
  }
  return { window: '24h', status: 'synthetic', amount: 0.34, currency: 'USD', basis: null, collected_at: null };
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
        reading: { ...absent, detail: 'Capture activity cannot be inferred from table suspension; it is not measured in this demonstration.' },
        delivery: { ...delivery, state: 'stale', observed_at: '2026-09-23T07:30:00Z', detail: 'Last synthetic delivery predates the suspension; no newer delivery is confirmed.' } };
    default:
      return base;
  }
}

const DEMO_NOW = '2026-09-23T09:30:00Z';

type DiagnosticStatus = 'passed' | 'failed' | 'unknown' | 'skipped';

function demoResult(fields: {
  readonly code: string; readonly layer: string; readonly status: DiagnosticStatus; readonly required: boolean;
  readonly observedAt: string; readonly provenance: string; readonly subject: Record<string, unknown> | null;
  readonly since?: string | null; readonly summary: string; readonly nextAction: string; readonly limits: string;
  readonly evidence?: Record<string, unknown>;
}): Record<string, unknown> {
  return {
    code: fields.code, layer: fields.layer, status: fields.status, required: fields.required,
    observed_at: fields.observedAt, provenance: fields.provenance, subject: fields.subject,
    since: fields.since ?? null, summary: fields.summary, next_action: fields.nextAction, limits: fields.limits,
    evidence: fields.evidence ?? {},
  };
}

const STAGE_CODES = [
  { code: 'capture.reading', layer: 'capture', key: 'reading', label: 'Journal capture' },
  { code: 'delivery.snowflake', layer: 'delivery', key: 'delivery', label: 'Snowflake delivery' },
  { code: 'reconciliation.destination', layer: 'reconciliation', key: 'reconciliation', label: 'Destination reconciliation' },
] as const;

/**
 * Synthetic control-plane diagnostic. It follows the real service's rules but
 * its evidence is simulated, so capture, delivery and reconciliation are
 * never `passed`: an observed simulated stage is `unknown`, a stale one is
 * `failed` from its last observation, an absent one is `unknown`.
 */
function demoDiagnostics(pipelines: readonly DemoPipeline[], pipelineId: string | null): Record<string, unknown> {
  const scoped = pipelineId === null ? pipelines : pipelines.filter((item) => item.id === pipelineId);
  const sourceIds = new Set(scoped.map((item) => item.sourceId));
  const results: Record<string, unknown>[] = [
    demoResult({
      code: 'control_plane.api', layer: 'control_plane', status: 'passed', required: true, observedAt: DEMO_NOW,
      provenance: 'control-plane:request', subject: { kind: 'control_plane', ref: 'control_plane', label: 'Control plane' },
      summary: 'The diagnostics route answered.', nextAction: 'No action.',
      limits: 'Synthetic: answered by the local demonstration, not by a real control plane.',
    }),
  ];
  for (const [sourceId, label, probed] of [[DEMO_SOURCE_SALES, 'Sales', true], [DEMO_SOURCE_INVENTORY, 'Inventory', false]] as const) {
    if (!sourceIds.has(sourceId)) continue;
    const subject = { kind: 'source', ref: sourceId, label };
    results.push(demoResult(probed ? {
      code: 'source.authentication', layer: 'authentication', status: 'passed', required: true,
      observedAt: '2026-09-23T08:00:00Z', provenance: 'control-plane:source-probe', subject,
      summary: 'A source probe signed on to IBM i and read its release.',
      nextAction: 'Run Test again after any change to the IBM i account or password.',
      limits: 'Synthetic: no IBM i system was queried.',
    } : {
      code: 'source.authentication', layer: 'authentication', status: 'unknown', required: true,
      observedAt: DEMO_NOW, provenance: 'control-plane:source-record', subject,
      summary: 'No probe has confirmed IBM i sign-on; configured credentials are not proof of authentication.',
      nextAction: 'Run Test for this source in the activation wizard.',
      limits: 'The control plane records detected values only after a successful probe.',
    }));
    results.push(demoResult({
      code: 'source.tls', layer: 'tls', status: 'unknown', required: true, observedAt: DEMO_NOW,
      provenance: 'control-plane:source-record', subject,
      summary: 'No probe has confirmed TLS with the IBM i host.',
      nextAction: 'Run Test for this source, or quadringent doctor --check-ibmi HOST --tls.',
      limits: 'No TLS observation is recorded for this source.',
    }));
    results.push(demoResult({
      code: 'destination.verification', layer: 'destination', status: 'passed', required: true, observedAt: DEMO_NOW,
      provenance: 'control-plane:destination-record',
      subject: { kind: 'destination', ref: destinationIdForSource(sourceId), label: `demo-${label.toLowerCase()}` },
      summary: 'The last access verification succeeded for the service role.',
      nextAction: 'Verify access again after changing roles or grants.',
      limits: 'Synthetic: no Snowflake system was queried.',
      evidence: { verification_state: 'verified' },
    }));
  }
  for (const pipeline of scoped) {
    const table = { kind: 'table', ref: `tbl_${pipeline.id}`, label: pipeline.name };
    const noKey = pipeline.id === 'pl_sales_archive';
    results.push(demoResult({
      code: 'source_prerequisites.table', layer: 'source_prerequisites', status: noKey ? 'unknown' : 'passed', required: true,
      observedAt: '2026-09-23T07:00:00Z', provenance: 'control-plane:table-discovery', subject: table,
      summary: noKey ? 'The table has no primary key or unique constraint.' : 'The table is journaled with before and after images and has a key.',
      nextAction: noKey ? 'Declare key columns, or acknowledge the RRN strategy and its reorganization risk.' : 'No action.',
      limits: 'Synthetic: readiness is generated, not read from an IBM i catalog.',
      evidence: { readiness: noKey ? 'no_key' : 'ready' },
    }));
    const subject = { kind: 'pipeline', ref: pipeline.id, label: pipeline.name };
    const attention = pipeline.declaredState === 'attention';
    results.push(demoResult({
      code: 'pipeline.state', layer: 'capture', status: attention ? 'failed' : 'passed', required: attention,
      observedAt: attention ? '2026-09-23T09:12:00Z' : DEMO_NOW, since: attention ? '2026-09-23T09:12:00Z' : null,
      provenance: 'control-plane:pipeline-record', subject,
      summary: attention ? 'The pipeline needs attention.' : `The pipeline is declared ${pipeline.declaredState}.`,
      nextAction: attention ? 'Open the table logs and the on-call runbook entry for this state before acting.' : 'No action.',
      limits: 'Declared state only; capture and delivery are separate results.',
      evidence: attention ? { declared_state: 'attention', attention_reason: 'Snowflake destination connection rejected (credentials).' } : { declared_state: pipeline.declaredState },
    }));
    const evidence = demoEvidence(pipeline) as Record<string, any>;
    const kind = String(evidence.evidence_kind);
    const states: string[] = [];
    for (const stage of STAGE_CODES) {
      const data = evidence[stage.key] as { state: string; observed_at: string | null; event_count: number | null };
      states.push(data.state);
      const common = { code: stage.code, layer: stage.layer, required: true, subject, provenance: `control-plane:evidence:${evidence.provenance}`,
        evidence: { stage_state: data.state, evidence_kind: kind, event_count: data.event_count } };
      if (data.state === 'stale') {
        results.push(demoResult({ ...common, status: 'failed', observedAt: data.observed_at ?? DEMO_NOW, since: data.observed_at,
          summary: `${stage.label} evidence is stale; nothing newer has been observed.`,
          nextAction: 'Open the table logs; check the reader and loader for this table before any action.',
          limits: 'The last observation time is shown; the current state is unknown.' }));
      } else if (data.state === 'observed') {
        results.push(demoResult({ ...common, status: 'unknown', observedAt: data.observed_at ?? DEMO_NOW,
          summary: `${stage.label} evidence is synthetic, not a current measurement.`,
          nextAction: 'Read live evidence from a running installation.',
          limits: 'Synthetic or historical evidence never proves the current state.' }));
      } else {
        results.push(demoResult({ ...common, status: 'unknown', observedAt: DEMO_NOW,
          summary: `${stage.label} has not been measured for this table.`,
          nextAction: 'Wait for the reader and loader to report evidence; check the table logs if nothing arrives.',
          limits: 'Absent evidence is not a failure and not a success.' }));
      }
    }
    const stale = states.includes('stale');
    results.push(demoResult({
      code: 'evidence.freshness', layer: 'evidence_freshness', status: stale ? 'failed' : 'unknown', required: true, observedAt: DEMO_NOW,
      provenance: `control-plane:evidence:${evidence.provenance}`, subject,
      summary: stale ? 'At least one evidence stage is stale; the current state is unknown.' : 'No current live evidence is available for this pipeline.',
      nextAction: 'Check the reader and loader for this table, then wait for new evidence.',
      limits: 'Freshness of recorded evidence only; it is not a reconciliation.',
      evidence: { evidence_kind: kind },
    }));
  }
  const count = (status: DiagnosticStatus) => results.filter((item) => item.status === status).length;
  const failed = count('failed');
  const requiredGap = results.some((item) => item.required === true && (item.status === 'unknown' || item.status === 'skipped'));
  const status = failed > 0 ? 'blocked' : requiredGap ? 'incomplete' : 'ready';
  const lead = failed > 0 ? `${failed} check(s) failed; fix them before relying on this installation.`
    : requiredGap ? 'Some required checks were not observed; their state is unknown.' : 'Every required check passed in this observation.';
  return {
    schema: 'quadringent.diagnostic.v1',
    generated_at: DEMO_NOW,
    scope: 'control_plane',
    synthetic: true,
    software: { product: 'quadringent', version: 'demonstration', distribution: 'control-plane' },
    overall: { status, passed: count('passed'), failed, unknown: count('unknown'), skipped: count('skipped'), summary: `${lead} Synthetic data: not a measurement of a real installation.` },
    results,
  };
}

/** Export of the synthetic report: nothing is pseudonymized or omitted because
 *  the report holds no real identifier, and the digests are computed here. */
function demoDiagnosticsExport(report: Record<string, unknown>): Record<string, unknown> {
  const limits = [
    'Built from diagnostic results only: no database, environment, log file or table row is included.',
    'Synthetic data: not a measurement of a real installation.',
  ];
  const text = (value: unknown) => `${JSON.stringify(value, null, 2)}\n`;
  const files: Record<string, string> = {
    'report.json': text(report),
    'README.txt': `Quadringent diagnostic export (synthetic)\n\nGenerated (UTC): ${DEMO_NOW}\n\n${limits.map((line) => `- ${line}`).join('\n')}\n`,
  };
  const listing = () => Object.entries(files).sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([name, content]) => ({ name, bytes: new TextEncoder().encode(content).length, sha256: sha256Hex(content) }));
  files['manifest.json'] = text({ schema: 'quadringent.diagnostic-export.v1', generated_at: DEMO_NOW, synthetic: true, files: listing(), limits });
  return {
    schema: 'quadringent.diagnostic-export.v1',
    generated_at: DEMO_NOW,
    preview: { files: listing(), omitted: [], pseudonymized: { hosts: 0, accounts: 0, identifiers: 0 }, limits },
    files,
  };
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
