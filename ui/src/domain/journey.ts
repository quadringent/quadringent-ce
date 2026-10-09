/**
 * Parcours de création d'une liaison.
 *
 * Il est écrit du point de vue de la personne qui l'exécute, pas du modèle
 * qu'il alimente. Chaque étape dit ce qu'elle demande et pourquoi, et ne
 * demande que ce qu'un exploitant peut fournir sans ouvrir de documentation.
 *
 * Deux honnêtetés structurent ce module :
 *
 * - Le service valide une configuration, il ne compose pas avec l'AS400. Le
 *   parcours ne promet donc jamais « connexion testée » : il dit « informations
 *   vérifiées », et annonce que la connexion réelle sera éprouvée à la mise en
 *   service. Promettre un test qui n'a pas lieu serait la pire des fausses
 *   données — celle qu'on ne découvre qu'en production.
 * - Le mot de passe n'entre jamais ici. Le parcours demande où il est déposé,
 *   jamais sa valeur.
 */

/** Les cinq moments du parcours, dans l'ordre où on les traverse. */
export const journeySteps = ['preparer', 'source', 'donnees', 'destination', 'creer'] as const;

export type JourneyStep = (typeof journeySteps)[number];

/**
 * Étape du contrat serveur à laquelle une étape du parcours est soumise.
 *
 * Le service valide de façon cumulative : demander la validation de
 * `destination` vaut validation de tout ce qui précède. Le parcours affiché et
 * le contrat validé restent ainsi distincts — on peut réécrire l'un sans
 * toucher l'autre.
 */
const SERVER_STEP: Readonly<Record<JourneyStep, string | null>> = {
  preparer: null,
  source: 'permissions',
  // Le service valide la sélection de tables en même temps que le suivi des
  // modifications, avant la destination : demander « où » avant « quoi »
  // faisait remonter, sur l'écran Snowflake, un reproche sur les tables que
  // l'opérateur n'avait pas encore eu l'occasion de choisir.
  donnees: 'journal',
  destination: 'destination',
  creer: 'verdict',
};

export function serverStepFor(step: JourneyStep): string | null {
  return SERVER_STEP[step];
}

export interface JourneyCopy {
  /** Ce qu'on fait, à l'infinitif de l'action attendue. */
  readonly title: string;
  /** Pourquoi on le fait — une phrase, jamais deux. */
  readonly why: string;
  /** Le libellé du bouton qui avance. */
  readonly next: string;
}

export const JOURNEY_COPY: Readonly<Record<JourneyStep, JourneyCopy>> = {
  preparer: {
    title: "Before you start",
    why: "You need three pieces of information. Gather them before continuing.",
    next: "Start",
  },
  source: {
    title: "Your AS400",
    why: "Quadringent needs to know which machine to read and which account to use.",
    next: "Continue",
  },
  destination: {
    title: "Where to store the data",
    why: "Your tables will be recreated in Snowflake at the location you specify here.",
    next: "Continue",
  },
  donnees: {
    title: "Which data to copy",
    why: "Choose tables to copy. You can add more later.",
    next: "Continue",
  },
  creer: {
    title: "Review and create",
    why: "Nothing is written until you confirm this page.",
    next: "Create connection",
  },
};

/** Ce que l'opérateur doit avoir sous la main avant de commencer. */
export interface Prerequisite {
  readonly label: string;
  readonly detail: string;
}

export const PREREQUISITES: readonly Prerequisite[] = [
  {
    label: "Your AS400 address",
    detail: "An IP address or hostname provided by your systems team.",
  },
  {
    label: "A reader account",
    detail: "An account allowed to read the tables to copy. A dedicated account is recommended.",
  },
  {
    label: "The password already stored",
    detail:
      "Quadringent never requests a password in this form. Your team has stored it in a vault; you only need its reference.",
  },
];

export function nextStep(step: JourneyStep): JourneyStep | null {
  const index = journeySteps.indexOf(step);
  return index >= 0 && index < journeySteps.length - 1 ? journeySteps[index + 1]! : null;
}

export function previousStep(step: JourneyStep): JourneyStep | null {
  const index = journeySteps.indexOf(step);
  return index > 0 ? journeySteps[index - 1]! : null;
}

/** Position dans le parcours, pour l'indicateur d'avancement. */
export interface JourneyMark {
  readonly step: JourneyStep;
  readonly label: string;
  readonly state: 'done' | 'current' | 'todo';
}

const MARK_LABEL: Readonly<Record<JourneyStep, string>> = {
  preparer: "Preparation",
  source: 'AS400',
  donnees: "Data",
  destination: 'Snowflake',
  creer: "Creation",
};

export function journeyMarks(current: JourneyStep): readonly JourneyMark[] {
  const currentIndex = journeySteps.indexOf(current);
  return journeySteps.map((step, index) => ({
    step,
    label: MARK_LABEL[step],
    state: index < currentIndex ? 'done' : index === currentIndex ? 'current' : 'todo',
  }));
}

/* ------------------------------------------------------------------ */
/* Ce que le parcours répond après une validation                      */
/* ------------------------------------------------------------------ */

export interface JourneyCheck {
  /** Vrai quand rien ne s'oppose au passage à l'étape suivante. */
  readonly passed: boolean;
  /** Ce qui manque ou bloque, en langage courant. Vide si tout va bien. */
  readonly problems: readonly string[];
  /** Ce qui reste à éprouver plus tard — dit sans le maquiller en garantie. */
  readonly pending: readonly string[];
}

/**
 * Traduit le verdict du service en réponse lisible.
 *
 * Le service distingue erreurs, blocages et points non prouvés. Les deux
 * premiers empêchent d'avancer et se disent ensemble : pour l'opérateur, un
 * champ manquant et une règle refusée sont le même problème — quelque chose à
 * corriger ici. Le troisième ne bloque pas mais ne doit pas être tu.
 */
export function checkFrom(verdict: {
  readonly errors: readonly string[];
  readonly blocked: readonly string[];
  readonly unproven: readonly string[];
}): JourneyCheck {
  const problems = [...verdict.errors, ...verdict.blocked].map(problemCopy);
  return {
    passed: problems.length === 0,
    problems,
    pending: pendingCopy(verdict.unproven),
  };
}

/* ------------------------------------------------------------------ */
/* Traduction des verdicts du service                                  */
/* ------------------------------------------------------------------ */

/**
 * Ce que le service refuse, dit à la personne qui peut le corriger.
 *
 * Les messages du service nomment son propre modèle — « hôte IBM i »,
 * « référence du secret Kubernetes ». Ils désignent pourtant des champs que
 * l'écran vient d'afficher sous d'autres noms : laisser passer le message
 * brut oblige l'opérateur à deviner quel champ reprendre.
 *
 * Un message inconnu n'est jamais avalé : il est affiché tel quel. Taire un
 * refus parce qu'on ne sait pas le traduire serait pire que le traduire mal.
 */
const PROBLEM_COPY: Readonly<Record<string, string>> = {
  "An IBM i host is required":
    "Enter your AS400 address.",
  "An IBM i user is required":
    "Enter the reader account.",
  "A Kubernetes secret reference is required":
    "Enter where the password is stored: its location name and entry name.",
  "TLS is required; plaintext IBM i connections are not allowed":
    "Your AS400 connection must be encrypted. It cannot be created otherwise.",
  "An IBM i TLS CA file is required":
    "The certificate that authenticates your AS400 is missing. Ask your operations team.",
  "An IBM i journal is required":
    "Change tracking is not enabled on this machine. Ask your systems team.",
  "Select tables to copy":
    "Choose at least one table to copy.",
  "The selection contains a duplicate table":
    "A table is selected twice.",
  "Unreadable table selection":
    "The table selection could not be read. Choose your data again.",
  "At least one configured table is outside the prepared scope":
    "One of the selected tables is unavailable on this machine.",
  "Destination disallowed by site policy":
    "This Snowflake destination is not allowed. Ask your operations team.",
  "A secret must never be sent to the control plane":
    "A password was entered in the form. This form does not accept passwords: only specify where it is stored.",
};

/**
 * Refus dont le texte porte des valeurs — un compte de tables, un nom de
 * schéma. Une table de correspondance exacte ne peut pas les couvrir : ils
 * passeraient tels quels, avec leur vocabulaire d'origine.
 */
const PROBLEM_PATTERNS: readonly (readonly [RegExp, (match: RegExpMatchArray) => string])[] = [
  [
    /^Snowflake destination not provisioned for (\d+) of the (\d+) selected tables$/,
    (match) =>
      `${match[1]} of the ${match[2]} selected tables do not yet have a prepared Snowflake location. Ask your operations team to prepare it.`,
  ],
  [
    /^This stage is not provisioned in /,
    () => "The destination location is not yet prepared for this environment. Ask your operations team.",
  ],
  [
    /^The Snowflake destination must remain (.+)$/,
    (match) => `This connection must write to ${match[1]}. Fix the database and schema.`,
  ],
  [
    /^The journal for this site is (.+)$/,
    (match) => `Data from this machine is read from ${match[1]}.`,
  ],
];

export function problemCopy(message: string): string {
  const exact = PROBLEM_COPY[message];
  if (exact !== undefined) return exact;
  for (const [pattern, render] of PROBLEM_PATTERNS) {
    const match = message.match(pattern);
    if (match) return render(match);
  }
  return message;
}

/**
 * Ce que le service n'a pas vérifié, dit sans le maquiller en garantie.
 *
 * Le parcours valide une configuration ; il ne compose pas avec l'AS400 et
 * n'écrit pas dans Snowflake. Ces réserves ne bloquent pas — elles disent à
 * quel moment la vérité sera faite.
 */
const PENDING_COPY: Readonly<Record<string, string>> = {
  "IBM i connectivity not observed":
    "The connection to your AS400 will be tested during activation.",
  "This form does not verify the Snowflake destination":
    "Snowflake access will be tested during activation.",
  "IBM i journaling and permissions unverified":
    "Read permissions will be tested during activation.",
  "This form does not verify pilot execution or safeguards":
    "The first copy's execution will be observed during activation.",
  "Server evidence and controlled start are not connected to this form":
    "Your operations team activates the connection; this form does not.",
};

/** Réserves à valeur variable, même traitement que les refus. */
const PENDING_PATTERNS: readonly (readonly [RegExp, (match: RegExpMatchArray) => string])[] = [
  [
    /^(\d+) table\(s\) without a business key/,
    (match) =>
      `${match[1]} of the selected tables have no unique identifier: reorganizing an AS400 file would require a full recopy.`,
  ],
];

/** Les réserves, dédoublonnées : plusieurs libellés disent la même attente. */
export function pendingCopy(messages: readonly string[]): readonly string[] {
  const seen = new Set<string>();
  for (const message of messages) {
    const exact = PENDING_COPY[message];
    if (exact !== undefined) {
      seen.add(exact);
      continue;
    }
    for (const [pattern, render] of PENDING_PATTERNS) {
      const match = message.match(pattern);
      if (match) {
        seen.add(render(match));
        break;
      }
    }
  }
  return [...seen];
}
