import { reading, unknown, type ConsoleSnapshot, type Flux, type ReaderPath } from '../../domain/types.ts';

/** Fixture entièrement synthétique. Le nom historique du fichier est conservé
 * pour les imports ; aucun relevé d'exploitation ni chiffre client n'est repris.
 * La date 2000 est celle du scénario vide, jamais une mesure runtime. */
const AT = '2000-01-01T00:00:00Z';
const SOURCE = "Empty synthetic fixture — no runtime measurement";
const REASON = "Not observed in this synthetic demonstration";

function emptyFlux(id: string, readerPath: ReaderPath): Flux {
  return {
    id,
    label: `Demonstration — ${readerPath}`,
    journal: 'DEMOJRN',
    journalLibrary: 'DEMOLIB',
    objects: ['SALE'],
    readerPath,
    target: "No destination connected",
    job: 'demo-unobserved',
    runState: reading('UNKNOWN', SOURCE, AT),
    runStartedAt: unknown(REASON, SOURCE, AT),
    position: {
      checkpoint: unknown(REASON, SOURCE, AT),
      sourceTail: unknown(REASON, SOURCE, AT),
      receiverFirstSequence: unknown(REASON, SOURCE, AT),
      receiverLastSequence: unknown(REASON, SOURCE, AT),
    },
    lag: {
      current: unknown(REASON, SOURCE, AT),
      verdict: unknown(REASON, SOURCE, AT),
      floorFirstThird: unknown(REASON, SOURCE, AT),
      floorLastThird: unknown(REASON, SOURCE, AT),
      max: unknown(REASON, SOURCE, AT),
      series: {
        source: SOURCE,
        observedAt: AT,
        points: [],
        sampleCount: 0,
        complete: false,
        incompleteBecause: REASON,
      },
    },
    counters: {
      polls: unknown(REASON, SOURCE, AT),
      errors: unknown(REASON, SOURCE, AT),
      windowsPublished: unknown(REASON, SOURCE, AT),
      eventsPublished: unknown(REASON, SOURCE, AT),
      eventsInTarget: unknown(REASON, SOURCE, AT),
      duplicatesInTarget: unknown(REASON, SOURCE, AT),
      receiverRotations: unknown(REASON, SOURCE, AT),
      meanMilliCpu: unknown(REASON, SOURCE, AT),
      cpuMsPerEvent: unknown(REASON, SOURCE, AT),
      runDurationS: unknown(REASON, SOURCE, AT),
    },
    timeline: [],
    caveats: ["Empty synthetic scenario: no qualified capture, replication or latency."],
  };
}

export const FIXTURE: ConsoleSnapshot = {
  fetchedAt: AT,
  flux: [emptyFlux('sale-rj', 'RetrieveJournal'), emptyFlux('sale-sql', 'DISPLAY_JOURNAL')],
};
