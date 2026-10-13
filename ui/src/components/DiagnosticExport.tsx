import { useEffect, useRef, useState } from 'react';
import { getCockpitClient } from '../data/useCockpit.ts';
import { DIAGNOSTIC_COPY } from '../domain/operator.ts';

type Phase = 'idle' | 'downloading' | 'started' | 'failed';

export function DiagnosticExport() {
  const [available, setAvailable] = useState(false);
  const [phase, setPhase] = useState<Phase>('idle');
  const activeDownload = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    getCockpitClient().then((client) => client.getDiagnosticExportCapability(controller.signal))
      .then((value) => { if (!controller.signal.aborted) setAvailable(value); })
      .catch(() => { if (!controller.signal.aborted) setAvailable(false); });
    return () => { controller.abort(); activeDownload.current?.abort(); };
  }, []);

  async function download() {
    if (!available || activeDownload.current !== null) return;
    const controller = new AbortController();
    activeDownload.current = controller;
    setPhase('downloading');
    try {
      const blob = await (await getCockpitClient()).getDiagnosticExport(controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = 'quadringent-diagnostic.json';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setPhase('started');
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 0);
      }
    } catch {
      if (!controller.signal.aborted) setPhase('failed');
    } finally {
      activeDownload.current = null;
    }
  }
  return <DiagnosticExportView available={available} phase={phase} onDownload={download} />;
}

export function DiagnosticExportView({ available, phase, onDownload }: {
  readonly available: boolean;
  readonly phase: Phase;
  readonly onDownload: () => void;
}) {
  if (!available) return null;
  return <div className="diagnostic-export">
    <button type="button" className="board__refresh" title={DIAGNOSTIC_COPY.scope} disabled={phase === 'downloading'} onClick={onDownload}>
      {phase === 'downloading' ? DIAGNOSTIC_COPY.preparing : DIAGNOSTIC_COPY.download}
    </button>
    {phase === 'failed' ? <span role="alert">{DIAGNOSTIC_COPY.failed}</span>
      : phase === 'started' ? <span role="status">{DIAGNOSTIC_COPY.started}</span> : null}
  </div>;
}
