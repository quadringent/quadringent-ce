import type { Flux } from './types';

/** « Est-ce que ça avance ? »
 *
 *  La seule question à laquelle toute vue doit répondre sans qu'on aille voir
 *  ailleurs. La réponse est dérivée de deux faits tenus séparés — l'état
 *  d'exécution et la trajectoire du plancher — parce que les confondre est
 *  précisément ce qui produit un statut « live » à côté de 7,9 millions
 *  d'événements de retard.
 */
export interface Answer {
  /** Oui, non, ou l'aveu qu'on ne sait pas. Jamais un quatrième cas. */
  readonly verdict: 'oui' | 'non' | "undetermined";
  /** La clause qui justifie, en cinq mots. */
  readonly because: string;
  /** Ce qu'il y a à faire. Vide quand il n'y a rien à faire. */
  readonly next?: string;
}

export function advanceAnswer(flux: Flux): Answer {
  const run = flux.runState.value;
  const lag = flux.lag.current.value;
  const trend = flux.lag.verdict.value;

  if (run === 'STOPPED_FAIL_CLOSED') {
    return {
      verdict: 'non',
      because: "stopped fail-closed",
      next:
        "The worker stopped rather than advance on a partial state. The " +
        "checkpoint is intact: restarting resumes from the last committed sequence.",
    };
  }
  if (run === 'STOPPED_AUTH_BLOCKED') {
    return {
      verdict: 'non',
      because: "source authentication blocked",
      next:
        "The source rejected the connection and the guard no longer attempts any " +
        "sign-on: fix the IBM i account, then reset the guard.",
    };
  }
  if (run === 'PAUSED_SOURCE') {
    return {
      verdict: 'non',
      because: "source paused",
      next:
        "The source is not responding (outage or maintenance). No attempt " +
        "is made before the deadline; processing resumes automatically from the checkpoint.",
    };
  }
  if (run === 'STOPPED_BUDGET') {
    return {
      verdict: 'non',
      because: "stopped, budget reached",
      next: "Restart the Job. The checkpoint resumes where processing stopped.",
    };
  }
  if (run === 'UNKNOWN' || run === null) {
    return { verdict: "undetermined", because: "execution state not observed" };
  }

  if (trend === 'DIVERGING') {
    return {
      verdict: 'non',
      because: "the floor is rising",
      next:
        "Lag no longer returns to the journal tail. Compare reader throughput with " +
        "journal production: sustained divergence indicates insufficient capacity, " +
        "rather than a temporary incident.",
    };
  }
  if (trend === null || trend === 'INCONCLUSIVE') {
    return { verdict: "undetermined", because: 'tendance non calculable' };
  }
  if (lag === null) {
    return { verdict: "undetermined", because: "lag cannot be calculated" };
  }
  if (trend === 'CATCHING_UP') {
    return { verdict: 'oui', because: "lag is decreasing" };
  }

  // BOUNDED couvre deux régimes très différents, et le verdict seul ne les
  // sépare pas : le seuil de pente de `lag_trend` vaut 0,25 × le retard moyen,
  // si bien qu'une descente linéaire depuis un très gros retard rend BOUNDED,
  // pas CATCHING_UP. C'est le plancher qui tranche, alors on le lit.
  const floorFirst = flux.lag.floorFirstThird.value;
  const floorLast = flux.lag.floorLastThird.value;
  if (floorFirst !== null && floorLast !== null && floorLast < floorFirst) {
    return { verdict: 'oui', because: "the floor is falling" };
  }

  return {
    verdict: 'oui',
    because: lag <= 1 ? "at the journal tail" : "lag is returning to the floor",
  };
}
