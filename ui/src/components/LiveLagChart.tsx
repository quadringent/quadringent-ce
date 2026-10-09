import type { LagPoint, Pipeline } from '../domain/controlPlane.ts';
import { lagTrend, VERDICT_COPY } from '../domain/lag.ts';
import { incidentOnset } from '../domain/liveBoard.ts';
import { buildLagSegments, lagCoverageLabel, selectSalientLagWindows } from '../domain/pipelineDetail.ts';
import { measureSubordination } from './proofStations.ts';
import { FactSpool } from './FactSpool.tsx';

const integerFormat = new Intl.NumberFormat("en-US");
const decimalFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

export interface LagTrend {
  readonly verdict: ReturnType<typeof lagTrend>['verdict'];
  readonly kind: 'bounded' | 'catching-up' | 'diverging' | 'inconclusive';
  readonly label: string;
  readonly answer: string;
  readonly delta: number | null;
}

export function resolveLagTrend(points: readonly LagPoint[]): LagTrend {
  const segment = buildLagSegments(points).at(-1) ?? [];
  const values = segment.map((point) => point.lag!);
  const trend = lagTrend(values);
  const copy = VERDICT_COPY[trend.verdict];
  const delta = values.length < 2 ? null : values[values.length - 1]! - values[0]!;
  return {
    verdict: trend.verdict,
    kind: trend.verdict.toLocaleLowerCase('en-US').replace('_', '-') as LagTrend['kind'],
    label: copy.label,
    answer: copy.answer,
    delta,
  };
}

export function LiveLagChart({ pipeline, current }: { readonly pipeline: Pipeline; readonly current: boolean }) {
  const points = pipeline.lagSeries;
  const trend = resolveLagTrend(points);
  const subordination = measureSubordination(pipeline);
  const currentLive = current && pipeline.quality.evidenceKind === 'live' && pipeline.quality.freshness === 'fresh' && subordination === null;
  const evidenceLabel = currentLive
    ? "Current reading"
    : pipeline.quality.evidenceKind === 'live'
      ? pipeline.quality.freshness === 'fresh'
        ? "Actual system · limited scope"
        : "Actual system · unusable observation"
    : pipeline.quality.evidenceKind === 'simulation'
      ? "Demonstration measurements"
      : "Earlier measurements";
  const lagLabel = currentLive ? "Current lag" : "Last observed lag";
  const trendLabel = currentLive ? "Trend" : "Trend of the last observed segment";
  const trendAnswer = currentLive && !subordination
    ? trend.answer
    : "Calculation limited to the last contiguous segment of this observation.";
  const lagValue = pipeline.lagSequences === null ? "Unknown" : integerFormat.format(pipeline.lagSequences);
  const [subordinationHeadline, subordinationHint] = splitSubordination(subordination);
  const lagUnit = pipeline.lagSequences === null
    ? ''
    : pipeline.lagSequences === 1 ? ' enregistrement' : " records";

  return (
    <section className={`live-evidence live-evidence--${pipeline.quality.evidenceKind}${currentLive ? ' live-evidence--fresh-live' : ' live-evidence--retained'}${subordination ? ' live-evidence--subordinated' : ''}`} aria-labelledby="lag-series-title">
      <FactSpool
        className="live-proof-band"
        label="Measurement scope"
        bands={[{
          key: 'quality',
          fields: [
            { label: "Scope", value: evidenceLabel, nowrap: true },
            { label: "Provenance", value: natureLabel(pipeline), nowrap: true },
            {
              label: "Observed",
              value: <>{freshnessLabel(pipeline)} · <time dateTime={pipeline.observedAt}>{formatTimestamp(pipeline.observedAt)}</time></>,
            },
            { label: "Limit", value: MEASURE_LIMIT },
          ],
        }]}
      />
      {subordination ? (
        <p className="live-measure-subordination">
          <strong>{subordinationHeadline}</strong>
          <span>last observed lag {lagValue}{lagUnit} — {subordinationHint}</span>
        </p>
      ) : (
        <div className="live-reading">
          <div className={`live-reading__trend live-reading__trend--${trend.kind}`}>
            <p className="section-kicker">{trendLabel}</p>
            <strong>{trend.label}</strong>
            <small>{trendAnswer}</small>
            {trend.delta === null ? null : <span className="live-reading__delta">Variation · {trend.delta > 0 ? '+' : ''}{integerFormat.format(trend.delta)} {Math.abs(trend.delta) === 1 ? 'enregistrement' : "records"}</span>}
          </div>
          <div className="live-reading__value">
            <p className="section-kicker">{lagLabel}</p>
            <p><strong>{lagValue}</strong>{pipeline.lagSequences === null ? null : <span>{pipeline.lagSequences === 1 ? 'enregistrement' : "records"}</span>}</p>
          </div>
        </div>
      )}
      {subordination ? (
        <p className={`live-measure-trend live-measure-trend--${trend.kind}`}>
          <span className="section-kicker">{trendLabel}</span>
          <strong>{trend.label}</strong>
          <small>{trendAnswer}</small>
        </p>
      ) : null}

      {points.length === 0 || pipeline.lagSeriesResolutionSeconds === null ? (
        <div className="live-evidence__empty">
          <p className="section-kicker">Source lag</p>
          <h2 id="lag-series-title">Series not measured</h2>
          <p>This observation provides no lag measurement; no curve is invented.</p>
        </div>
      ) : <LagGraph pipeline={pipeline} points={points} subordinated={subordination !== null} />}
    </section>
  );
}

function LagGraph({ pipeline, points, subordinated }: { readonly pipeline: Pipeline; readonly points: readonly LagPoint[]; readonly subordinated: boolean }) {
  const width = 960;
  const height = 180;
  const padding = { left: 32, right: 16, top: 14, bottom: 22 };
  const maxEnd = Math.max(...points.map((point) => point.endSeconds), 1);
  const observedMax = Math.max(...points.flatMap((point) => [point.high ?? 0, point.lag ?? 0]), 0);
  const maxLag = Math.max(observedMax, 1);
  const x = (point: LagPoint) => padding.left + ((point.startSeconds + point.endSeconds) / 2 / maxEnd) * (width - padding.left - padding.right);
  const y = (value: number) => {
    const normalized = Math.log10(value + 1) / Math.log10(maxLag + 1);
    return padding.top + (1 - normalized) * (height - padding.top - padding.bottom);
  };
  const segments = buildLagSegments(points);
  const floorNote = floorAnnotation(pipeline, subordinated);
  // Début de l'incident servi — texte, pas marqueur positionné : l'axe du
  // tracé est en secondes relatives, sans origine temporelle publiée.
  const onset = pipeline.incident !== null ? incidentOnset(pipeline, pipeline.incident.type) : null;
  const preview = selectSalientLagWindows(points);
  const previewPoints = preview.points;
  const previewIsComplete = preview.complete;

  return (
    <>
      <div className="lag-chart__head">
        <div><p className="section-kicker">Continuous measurement</p><h2 id="lag-series-title">Source lag, without interpolation</h2></div>
        <p>One measurement every {decimalFormat.format(pipeline.lagSeriesResolutionSeconds!)} s · {integerFormat.format(pipeline.lagSampleCount)} measurements</p>
      </div>
      {pipeline.lagUnknownSampleCount > 0 ? <p className="live-evidence__warning">{integerFormat.format(pipeline.lagUnknownSampleCount)} unknown measurements remain explicitly empty.</p> : null}
      {floorNote ? <p className="lag-chart__floor">{floorNote}</p> : null}
      {onset !== null ? (
        <p className="lag-chart__onset">
          Reported incident — {onset.word} since <time dateTime={onset.at}>{formatTimestamp(onset.at)}</time>
        </p>
      ) : null}
      <div className="lag-chart" aria-label="Lag in records, logarithmic vertical scale, without interpolation of missing data.">
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
          <line className="lag-chart__axis" vectorEffect="non-scaling-stroke" x1={padding.left} y1={height - padding.bottom} x2={width - padding.right} y2={height - padding.bottom} />
          {points.filter((point) => point.coverage === 'complete' && point.low !== null && point.high !== null).map((point) => (
            <line className="lag-chart__range" vectorEffect="non-scaling-stroke" key={`${point.startSeconds}-${point.endSeconds}`} x1={x(point)} x2={x(point)} y1={y(point.high!)} y2={y(point.low!)} />
          ))}
          {segments.map((segment, index) => segment.length === 1 ? (
            <circle className="lag-chart__point" vectorEffect="non-scaling-stroke" key={`segment-${index}`} cx={x(segment[0]!)} cy={y(segment[0]!.lag!)} r="4" />
          ) : (
            <polyline className="lag-chart__line" vectorEffect="non-scaling-stroke" key={`segment-${index}`} points={segment.map((point) => `${x(point)},${y(point.lag!)}`).join(' ')} />
          ))}
        </svg>
        <div className="lag-chart__scale"><span>0 s</span><span>max {integerFormat.format(observedMax)}</span><span>{decimalFormat.format(maxEnd)} s</span></div>
      </div>
      <div className="lag-chart__guide">
        <p>Logarithmic vertical scale · lag in records</p>
        <ul className="lag-chart__legend" aria-label="Chart legend">
          <li><span className="lag-chart__key lag-chart__key--line" aria-hidden="true" />Last lag</li>
          <li><span className="lag-chart__key lag-chart__key--range" aria-hidden="true" />Minimum–maximum range</li>
        </ul>
      </div>
      <div className="lag-register-preview">
        <LagRegisterTable
          points={previewPoints}
          caption={preview.caption}
        />
      </div>
      {previewIsComplete ? null : (
        <details className="lag-register">
          <summary>View {integerFormat.format(points.length)} measurement intervals</summary>
          <div className="lag-table-wrap">
            <LagRegisterTable points={points} caption="All intervals, synchronized with the chart" />
          </div>
        </details>
      )}
    </>
  );
}

function LagRegisterTable({ points, caption }: { readonly points: readonly LagPoint[]; readonly caption: string }) {
  return (
    <table className="lag-table">
      <caption>{caption}</caption>
      <thead><tr><th scope="col">Time</th><th scope="col">Last</th><th scope="col">Minimum</th><th scope="col">Maximum</th><th scope="col">Measurements</th><th scope="col">Coverage</th></tr></thead>
      <tbody>{points.map((point) => (
        <tr key={`${point.startSeconds}-${point.endSeconds}`} className={point.coverage === 'gap' ? 'is-gap' : undefined}>
          <th scope="row">{decimalFormat.format(point.startSeconds)}–{decimalFormat.format(point.endSeconds)} s</th>
          <td data-label="Last">{formatLag(point.lag)}</td>
          <td data-label="Minimum">{formatLag(point.low)}</td>
          <td data-label="Maximum">{formatLag(point.high)}</td>
          <td data-label="Measurements">{integerFormat.format(point.samples)}</td>
          <td data-label="Coverage">{lagCoverageLabel(point)}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function floorAnnotation(pipeline: Pipeline, subordinated: boolean): string | null {
  if (!subordinated) return null;
  if (pipeline.lagSequences === 0 || pipeline.lagSeconds === 0) {
    return "Lag at 0 — subordinate measurement";
  }
  return null;
}

const MEASURE_LIMIT = "Reading and its volumes do not confirm arrival at the destination.";

function splitSubordination(copy: string | null): readonly [string, string] {
  if (!copy) return ['', ''];
  const [headline, ...rest] = copy.split(', ');
  return [headline ?? copy, rest.join(', ') || "not a current health signal"];
}

function natureLabel(pipeline: Pipeline): string {
  return { live: "Actual system", historical: "Earlier observation", simulation: "Demonstration" }[pipeline.quality.evidenceKind];
}

function freshnessLabel(pipeline: Pipeline): string {
  if (pipeline.quality.freshness === 'fresh') {
    if (pipeline.quality.evidenceKind === 'historical') return "Recent at the earlier observation time · current freshness not established";
    if (pipeline.quality.evidenceKind === 'simulation') return "Recent in the demonstration · actual freshness not established";
    return "Recent observation";
  }
  return { late: "Late observation", stale: "Observation too old", clock_untrusted: "Clock unreliable" }[pipeline.quality.freshness];
}

function formatLag(value: number | null): string {
  return value === null ? "Unknown" : integerFormat.format(value);
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-US", { hourCycle: 'h23', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }).format(new Date(value));
}
