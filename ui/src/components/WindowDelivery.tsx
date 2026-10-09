import type { WindowDelivery as WindowEvidence } from '../domain/controlPlane.ts';

const dates = new Intl.DateTimeFormat("en-US", { hourCycle: 'h23', dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' });
const provenance = { live: "Actual observation", historical: "Historical evidence", simulation: 'Simulation' };
const freshness = { fresh: "Fresh observation", late: "Late observation", stale: "Old evidence", clock_untrusted: "Clock unreliable" };

export function WindowDelivery({ evidence, cached }: { readonly evidence: WindowEvidence | null | undefined; readonly cached: boolean }) {
  if (!evidence) return null;
  const chain=evidence.chain;
  const progress=chain ? <p>{chain.matchedWindows} out of {chain.declaredWindows} windows with reconciliation evidence · {provenance[chain.evidenceKind]}{cached ? " · Last retained state" : ''}</p> : null;
  if (evidence.state === 'invalid' || evidence.state === 'unavailable') {
    return <section className="window-delivery" aria-label="Window evidence">
      <h2>{evidence.state === 'invalid' ? "Invalid window evidence" : chain ? "Incomplete evidence chain" : "Window evidence unavailable"}</h2>
      {progress}
      <p>{chain ? "Not every window is qualified. No missing window is ignored." : "Reconciliation cannot be established for this window."} Refresh the evidence; capture state remains separate.</p>
    </section>;
  }
  if (!('quality' in evidence)) return null;
  return <section className="window-delivery" aria-label="Window evidence">
    <p className="section-kicker">Closed window · {provenance[evidence.quality.evidenceKind]} · {cached ? "Retained snapshot" : freshness[evidence.quality.freshness]}</p>
    {progress}
    <h2>{evidence.state === 'matched' ? `${new Intl.NumberFormat("en-US").format(evidence.eventCount)} ${evidence.eventCount === 1 ? "reconciled event" : "reconciled events"}` : "Empty window — delivery untested"}</h2>
    <p>From <time dateTime={evidence.startedAt}>{dates.format(new Date(evidence.startedAt))}</time> to <time dateTime={evidence.closedAt}>{dates.format(new Date(evidence.closedAt))}</time> UTC.</p>
    <p>This evidence covers only this window, not current capture or subsequent events.</p>
    {chain && <p>The event count above covers only the last displayed window, not the entire chain.</p>}
    <details><summary>Reconciliation provenance</summary>
      <p>Archive {evidence.archiveRunId} · window {evidence.windowId}</p>
      <p>Destination observed on <time dateTime={evidence.destinationObservedAt}>{dates.format(new Date(evidence.destinationObservedAt))}</time> UTC.</p>
    </details>
  </section>;
}
