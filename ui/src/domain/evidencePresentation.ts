import type { EvidenceStage, PipelineEvidence } from '../data/controlPlaneV2Client.ts';
import { caveatFor, EVIDENCE_COPY } from './operator.ts';

/** observed: a current observation; limit: synthetic, historical or stale;
 *  missing: nothing measured. Only `observed` may use the green signal. */
export type EvidenceTone = 'observed' | 'limit' | 'missing';

export interface EvidenceKindView {
  readonly label: string;
  readonly tone: EvidenceTone;
  readonly caveat: string | null;
}

export interface EvidenceStageView {
  readonly word: string;
  readonly tone: EvidenceTone;
  readonly observedAt: string | null;
}

export function evidenceKindView(kind: PipelineEvidence['evidenceKind']): EvidenceKindView {
  switch (kind) {
    case 'live':
      return { label: EVIDENCE_COPY.kinds.live, tone: 'observed', caveat: null };
    case 'historical':
      return { label: EVIDENCE_COPY.kinds.historical, tone: 'limit', caveat: caveatFor('historical', false) };
    case 'simulation':
      return { label: EVIDENCE_COPY.kinds.simulation, tone: 'limit', caveat: caveatFor('simulation', false) };
    case 'absent':
      return { label: EVIDENCE_COPY.kinds.absent, tone: 'missing', caveat: EVIDENCE_COPY.absentKindDetail };
  }
}

export function evidenceStageView(stage: EvidenceStage, kind: PipelineEvidence['evidenceKind']): EvidenceStageView {
  if (stage.state === 'absent') return { word: EVIDENCE_COPY.words.missing, tone: 'missing', observedAt: null };
  if (stage.state === 'stale') {
    const word = kind === 'simulation' ? EVIDENCE_COPY.words.syntheticStale
      : kind === 'historical' ? EVIDENCE_COPY.words.historicalStale : EVIDENCE_COPY.words.stale;
    return { word, tone: 'limit', observedAt: stage.observedAt };
  }
  if (kind === 'simulation') return { word: EVIDENCE_COPY.words.synthetic, tone: 'limit', observedAt: stage.observedAt };
  if (kind === 'historical') return { word: EVIDENCE_COPY.words.historical, tone: 'limit', observedAt: stage.observedAt };
  if (kind === 'absent') return { word: EVIDENCE_COPY.words.missing, tone: 'missing', observedAt: null };
  return { word: EVIDENCE_COPY.words.observed, tone: 'observed', observedAt: stage.observedAt };
}
