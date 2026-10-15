import { useEffect, useRef, useState } from 'react';
import { createCockpitClient, createCockpitResolvers } from './cockpitClient.ts';
import type { ControlPlaneV2Client } from './controlPlaneV2Client.ts';
import type { CockpitConnection } from '../domain/cockpit.ts';
import { runCancellable } from './cancellableLoad.ts';
import {
  loadConnections,
  loadHomeRows,
  loadTableData,
  tableDataMatches,
  type CockpitTableData,
  type CockpitTableLogFilters,
  type ConnectionHomeRow,
} from './cockpitLoaders.ts';

export type { CockpitTableData, CockpitTableLogFilters, ConnectionHomeRow } from './cockpitLoaders.ts';

/** Mémorise le client (démo ou réel) : un seul import dynamique par session,
 *  jamais de course entre deux appels concurrents qui créeraient chacun un
 *  client démo indépendant avec son propre état muté en mémoire. */
let cockpitClientPromise: Promise<ControlPlaneV2Client> | null = null;
export function getCockpitClient(): Promise<ControlPlaneV2Client> {
  if (!cockpitClientPromise) cockpitClientPromise = createCockpitClient();
  return cockpitClientPromise;
}

/**
 * Charge les connexions du cockpit (sources + destinations + pipelines,
 * assemblés par `domain/cockpit.ts::buildConnections`). Le client réel vient
 * de `createCockpitClient()` (démo en dev sans backend, sinon `/v2`) — voir
 * ce fichier pour pourquoi l'import est asynchrone.
 *
 * Chaque effet possède son propre drapeau et son `AbortController`
 * (`cancellableLoad.ts`) : la réponse d'une sélection précédente ne peut
 * jamais s'afficher après celle de la sélection suivante.
 */
export type CockpitConnectionsState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly connections: readonly CockpitConnection[] }
  | { readonly status: 'failed'; readonly message: string };

/** `reload` shows the loading state; `refresh` re-reads while the current
 *  data stays on screen, so a controls panel keeps its outcome message. */
export interface Reloadable {
  readonly reload: () => void;
  readonly refresh: () => void;
}

export function useCockpitConnections(): { readonly state: CockpitConnectionsState } & Reloadable {
  const [state, setState] = useState<CockpitConnectionsState>({ status: 'loading' });
  const [nonce, setNonce] = useState(0);
  const quiet = useRef(false);

  useEffect(() => {
    if (!quiet.current) setState({ status: 'loading' });
    quiet.current = false;
    return runCancellable(
      async (signal) => loadConnections(await getCockpitClient(), createCockpitResolvers, signal),
      (outcome) => setState(outcome.status === 'ready'
        ? { status: 'ready', connections: outcome.value }
        : { status: 'failed', message: outcome.message }),
    );
  }, [nonce]);

  return {
    state,
    reload: () => setNonce((n) => n + 1),
    refresh: () => { quiet.current = true; setNonce((n) => n + 1); },
  };
}

export type CockpitHomeState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly rows: readonly ConnectionHomeRow[] }
  | { readonly status: 'failed'; readonly message: string };

/**
 * Vue Accueil : une ligne par connexion, avec retard/débit agrégés (dernier
 * point 1 h de chaque table) et le coût du jour de la connexion
 * (`GET /v2/costs?scope=connection`). Une requête de plus par connexion et
 * par table que `useCockpitConnections` seul ; acceptable pour le nombre de
 * connexions/tables attendu (dizaines, pas milliers).
 */
export function useCockpitHome(): { readonly state: CockpitHomeState } & Reloadable {
  const { state: connectionsState, reload, refresh } = useCockpitConnections();
  const [state, setState] = useState<CockpitHomeState>({ status: 'loading' });

  useEffect(() => {
    if (connectionsState.status === 'loading') {
      setState({ status: 'loading' });
      return undefined;
    }
    if (connectionsState.status === 'failed') {
      setState({ status: 'failed', message: connectionsState.message });
      return undefined;
    }
    return runCancellable(
      async (signal) => loadHomeRows(await getCockpitClient(), connectionsState.connections, signal),
      (outcome) => setState(outcome.status === 'ready'
        ? { status: 'ready', rows: outcome.value }
        : { status: 'failed', message: outcome.message }),
    );
  }, [connectionsState]);

  return { state, reload, refresh };
}

export type CockpitTableState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly data: CockpitTableData }
  | { readonly status: 'failed'; readonly message: string };

/**
 * Charge le détail d'une table du cockpit : pipeline (`declared_state`),
 * métriques (fenêtre `window`), journaux (`logFilters`), coûts du jour. Les
 * quatre routes viennent du client v2 — voir controlPlaneV2Client.ts pour
 * lesquelles sont CONTRAT SEUL au moment de l'écrit.
 */
export function useCockpitTable(
  pipelineId: string,
  window: '1h' | '24h',
  logFilters: CockpitTableLogFilters,
): { readonly state: CockpitTableState } & Reloadable {
  const [state, setState] = useState<CockpitTableState>({ status: 'loading' });
  const [nonce, setNonce] = useState(0);
  const quiet = useRef(false);
  const loadedFor = useRef(pipelineId);

  useEffect(() => {
    // A quiet refresh keeps the screen only for the same table: a new
    // selection always starts from the loading state.
    const keep = quiet.current && loadedFor.current === pipelineId;
    quiet.current = false;
    loadedFor.current = pipelineId;
    if (!keep) setState({ status: 'loading' });
    return runCancellable(
      async (signal) => loadTableData(await getCockpitClient(), createCockpitResolvers, pipelineId, window, logFilters, signal),
      (outcome) => setState(outcome.status === 'ready'
        ? { status: 'ready', data: outcome.value }
        : { status: 'failed', message: outcome.message }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the filter object is rebuilt each render; its two fields are the real dependencies.
  }, [pipelineId, window, logFilters.level, logFilters.correlateIncident, nonce]);

  const visible: CockpitTableState = state.status === 'ready' && !tableDataMatches(state.data, pipelineId)
    ? { status: 'loading' }
    : state;
  return {
    state: visible,
    reload: () => setNonce((n) => n + 1),
    refresh: () => { quiet.current = true; setNonce((n) => n + 1); },
  };
}
