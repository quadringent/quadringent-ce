/**
 * Assistant v2 — étape « Tables » (docs/plans/2026-09-23-control-plane-v2-contract.md §2.3).
 *
 * `readiness` et `key_strategy` sont déjà calculés côté serveur
 * (`GET /v2/sources/{id}/tables`, `services/tables.py::TableRecord.to_dict`
 * / `quadringent.table_discovery.classify_table`) : cet écran ne recalcule
 * jamais une disponibilité lui-même, il traduit ce que le service renvoie en
 * un mot lisible (« prête / non journalisée / images incomplètes / sans
 * clé / journal incohérent »), à la manière de StatusWord.
 */

export type TableReadiness = 'ready' | 'not_journaled' | 'images_incomplete' | 'no_key' | 'journal_mismatch';
export type KeyStrategy = 'primary' | 'unique_index' | 'rrn';

export interface DiscoveredTable {
  readonly id: string;
  readonly library: string;
  readonly name: string;
  readonly approxRowCount: number;
  readonly approxSizeBytes: number;
  readonly readiness: TableReadiness;
  readonly keyStrategy: KeyStrategy;
  /** Colonnes de la clé déjà connue (index unique détecté par la
   *  découverte, ou choisie par un précédent `PATCH`). Vide tant qu'aucune
   *  clé n'a été trouvée ni déclarée. */
  readonly keyColumns: readonly string[];
  /** Commandes CL suggérées pour lever le blocage (journal absent/incomplet). Vide si rien à corriger. */
  readonly clFixCommands: readonly string[];
}

export interface TableReadinessCopy {
  readonly label: string;
  readonly description: string;
}

const READINESS_COPY: Readonly<Record<TableReadiness, TableReadinessCopy>> = {
  ready: {
    label: "Ready",
    description: "The journal contains before/after images and a usable key is available.",
  },
  not_journaled: {
    label: "Not journaled",
    description: "An IBM i journal is required to capture this table; it is missing.",
  },
  images_incomplete: {
    label: "Incomplete images",
    description: "The journal exists but does not retain the before/after images required for capture.",
  },
  no_key: {
    label: "No key",
    description: "No unique key is configured: choose a key before starting.",
  },
  journal_mismatch: {
    label: "Inconsistent journal",
    description: "The configured journal differs from the one detected on IBM i: verify before starting.",
  },
};

/** Traduction directe de l'état serveur — jamais recalculée. */
export function evaluateTableReadiness(table: DiscoveredTable): TableReadiness {
  return table.readiness;
}

export function tableReadinessCopy(state: TableReadiness): TableReadinessCopy {
  return READINESS_COPY[state];
}

export function requiresKeyChoice(table: DiscoveredTable): boolean {
  return table.readiness === 'no_key';
}

/**
 * Choisir la RRN (position physique) comme clé de remplacement a une
 * conséquence explicite sur la reprise (docs §294 : "sans clé métier :
 * identifiées par leur position physique (RRN)") — elle doit être
 * acquittée, jamais cochée par défaut.
 */
export function requiresRrnAcknowledgement(chosenKey: KeyStrategy): boolean {
  return chosenKey === 'rrn';
}

export const RRN_CONSEQUENCE_SENTENCE =
  "Without a business key, rows are identified by physical position (RRN): reorganizing the IBM i table may invalidate change tracking.";

/**
 * ``choice`` porte la stratégie choisie par l'opérateur pour une table
 * ``no_key`` (jamais pour une table déjà prête). ``columns`` est requis et
 * non vide pour ``unique_index`` (``services/tables.py::choose_key`` rejette
 * sinon), ``rrnAcknowledged`` requis pour ``rrn``.
 */
export function isTableStartable(
  table: DiscoveredTable,
  choice?: { readonly key: Exclude<KeyStrategy, 'primary'>; readonly columns: readonly string[]; readonly rrnAcknowledged: boolean },
): boolean {
  const readiness = evaluateTableReadiness(table);
  if (readiness === 'not_journaled' || readiness === 'images_incomplete' || readiness === 'journal_mismatch') return false;
  if (readiness === 'no_key') {
    if (!choice) return false;
    if (choice.key === 'unique_index') return choice.columns.length > 0;
    if (choice.key === 'rrn') return choice.rrnAcknowledged;
    return false;
  }
  return true;
}

/** Bibliothèques système explicites : aucune déduction depuis le journal. */
export function parseDiscoveryLibraries(value: string): readonly string[] | null {
  const names = value.split(',').map((name) => name.trim().toUpperCase());
  if (names.length === 0 || names.some((name) => !/^[A-Z0-9_$#@]{1,10}$/.test(name))) return null;
  return [...new Set(names)];
}
