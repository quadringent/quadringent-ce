import { useCallback, useEffect, useRef, useState } from 'react';
import { runCancellable } from './cancellableLoad.ts';
import { getCockpitClient } from './useCockpit.ts';
import type { DiagnosticExport, DiagnosticReport } from './diagnosticsContract.ts';
import { describeDiagnosticsError, type DiagnosticsFailure } from '../domain/diagnostics.ts';
import { checkExportIntegrity, type ExportIntegrity } from '../domain/diagnosticsExport.ts';

export type DiagnosticsState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly report: DiagnosticReport; readonly pipelineId: string | null }
  | { readonly status: 'failed'; readonly failure: DiagnosticsFailure };

/**
 * Reads `GET /v2/diagnostics/report`. Each selection (all pipelines, or one) owns its
 * flag and abort signal: a slow report for pipeline A can never be shown once
 * the screen moved to pipeline B. Reading changes nothing on the service.
 */
export function useDiagnostics(pipelineId: string | null): { readonly state: DiagnosticsState; readonly reload: () => void } {
  const [state, setState] = useState<DiagnosticsState>({ status: 'loading' });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    setState({ status: 'loading' });
    return runCancellable(
      async (signal) => (await getCockpitClient()).getDiagnostics(pipelineId ? { pipelineId } : {}, signal),
      (outcome) => setState(outcome.status === 'ready'
        ? { status: 'ready', report: outcome.value, pipelineId }
        : { status: 'failed', failure: describeDiagnosticsError(outcome.error) }),
    );
  }, [pipelineId, nonce]);

  // Never show a report that was read for another selection.
  const visible: DiagnosticsState = state.status === 'ready' && state.pipelineId !== pipelineId ? { status: 'loading' } : state;
  return { state: visible, reload: () => setNonce((n) => n + 1) };
}

export type DiagnosticsExportState =
  | { readonly status: 'idle' }
  | { readonly status: 'preparing' }
  | { readonly status: 'ready'; readonly exported: DiagnosticExport; readonly integrity: ExportIntegrity }
  | { readonly status: 'failed'; readonly failure: DiagnosticsFailure };

/**
 * Prepares `GET /v2/diagnostics/report/export` in memory only. The preview stays on
 * screen until the operator downloads or discards it; nothing is persisted.
 */
export function useDiagnosticsExport(): {
  readonly state: DiagnosticsExportState;
  readonly prepare: () => void;
  readonly discard: () => void;
} {
  const [state, setState] = useState<DiagnosticsExportState>({ status: 'idle' });
  const cancelRef = useRef<(() => void) | null>(null);

  const cancel = useCallback(() => {
    cancelRef.current?.();
    cancelRef.current = null;
  }, []);

  useEffect(() => cancel, [cancel]);

  const prepare = useCallback(() => {
    cancel();
    setState({ status: 'preparing' });
    cancelRef.current = runCancellable(
      async (signal) => {
        const exported = await (await getCockpitClient()).getDiagnosticsExport(signal);
        return { exported, integrity: await checkExportIntegrity(exported) };
      },
      (outcome) => setState(outcome.status === 'ready'
        ? { status: 'ready', exported: outcome.value.exported, integrity: outcome.value.integrity }
        : { status: 'failed', failure: describeDiagnosticsError(outcome.error) }),
    );
  }, [cancel]);

  const discard = useCallback(() => {
    cancel();
    setState({ status: 'idle' });
  }, [cancel]);

  return { state, prepare, discard };
}
