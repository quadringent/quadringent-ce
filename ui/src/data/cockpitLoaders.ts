/**
 * Data loaders of the cockpit screens, independent of React and of the
 * demo/real client choice so a selection race can be tested without a DOM.
 * Every request receives the caller's `AbortSignal`; the caller (see
 * `cancellableLoad.ts`) decides whether the outcome is still the current one.
 */
import type { CockpitPipelineResolvers } from './cockpitClient.ts';
import type {
  ControlPlaneV2Client,
  CostSnapshot,
  LogEntry,
  LogLevel,
  MetricsSeries,
  PipelineEvidence,
  PipelineListRecordV2,
  PipelineRecordV2,
} from './controlPlaneV2Client.ts';
import {
  aggregateConnectionMetrics,
  buildConnections,
  groupTablesBySource,
  type CockpitConnection,
} from '../domain/cockpit.ts';

export type ResolversFactory = (
  client: ControlPlaneV2Client,
  pipelines: readonly PipelineListRecordV2[],
) => Promise<CockpitPipelineResolvers>;

export interface ConnectionHomeRow {
  readonly connection: CockpitConnection;
  readonly lagSeconds: number | null;
  readonly throughputRowsPerSecond: number | null;
  readonly costToday: CostSnapshot | null;
}

export interface CockpitTableData {
  readonly pipeline: PipelineRecordV2;
  readonly metrics: MetricsSeries;
  readonly logs: readonly LogEntry[];
  readonly costToday: CostSnapshot | null;
  readonly evidence?: PipelineEvidence | null;
  readonly evidenceError?: string;
  /** Display name (library/table); the screen falls back to the pipeline id
   *  when the catalogue is momentarily unavailable. */
  readonly name?: string;
}

export interface CockpitTableLogFilters {
  readonly level?: LogLevel;
  readonly correlateIncident?: boolean;
}

export async function loadConnections(
  client: ControlPlaneV2Client,
  makeResolvers: ResolversFactory,
  signal?: AbortSignal,
): Promise<readonly CockpitConnection[]> {
  const [sources, destinations, pipelines] = await Promise.all([
    client.listSources(signal),
    client.listDestinations(signal),
    client.listPipelines(signal),
  ]);
  const resolvers = await makeResolvers(client, pipelines);
  const grouped = groupTablesBySource(pipelines, resolvers.sourceIdForPipeline, resolvers.nameForPipeline);
  return buildConnections(sources, destinations, grouped);
}

export async function loadHomeRows(
  client: ControlPlaneV2Client,
  connections: readonly CockpitConnection[],
  signal?: AbortSignal,
): Promise<readonly ConnectionHomeRow[]> {
  return Promise.all(connections.map(async (connection): Promise<ConnectionHomeRow> => {
    const [points, costToday] = await Promise.all([
      Promise.all(connection.tables.map(async (table) => {
        const series = await client.getMetrics(table.pipelineId, '1h', signal);
        return series.points.at(-1) ?? null;
      })),
      client.getCosts('connection', connection.id, '24h', signal).catch(() => null),
    ]);
    const aggregate = aggregateConnectionMetrics(points);
    return { connection, lagSeconds: aggregate.lagSeconds, throughputRowsPerSecond: aggregate.throughputRowsPerSecond, costToday };
  }));
}

export async function loadTableData(
  client: ControlPlaneV2Client,
  makeResolvers: ResolversFactory,
  pipelineId: string,
  window: '1h' | '24h',
  logFilters: CockpitTableLogFilters,
  signal?: AbortSignal,
): Promise<CockpitTableData> {
  const [pipeline, metrics, logs, costToday, pipelines, evidenceResult] = await Promise.all([
    client.getPipeline(pipelineId, signal),
    client.getMetrics(pipelineId, window, signal),
    client.getLogs(pipelineId, logFilters, signal),
    client.getCosts('table', pipelineId, window, signal).catch(() => null),
    client.listPipelines(signal).catch(() => [] as readonly PipelineListRecordV2[]),
    client.getEvidence(pipelineId, signal).then((evidence) => ({ evidence, evidenceError: undefined }))
      .catch((error: unknown) => ({ evidence: null, evidenceError: error instanceof Error ? error.message : 'Evidence unavailable.' })),
  ]);
  const resolvers = await makeResolvers(client, pipelines);
  const name = resolvers.nameForPipeline(pipelineId);
  return { pipeline, metrics, logs, costToday, ...evidenceResult, name: name === pipelineId ? undefined : name };
}

/** Data loaded for another table must never be shown under the current
 *  selection, not even for the frame between the route change and the effect
 *  that starts the new load. */
export function tableDataMatches(data: CockpitTableData, pipelineId: string): boolean {
  return data.pipeline.id === pipelineId;
}
