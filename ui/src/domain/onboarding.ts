import { formatDuration, sequences } from './format.ts';
import { parseSiteIdentity, type SiteIdentity } from './siteIdentity.ts';

export const onboardingSteps = [
  'source',
  'permissions',
  'journal',
  'destination',
  'pilot',
  'verdict',
  'activate',
] as const;

export type OnboardingStep = (typeof onboardingSteps)[number];
export type OnboardingStatus = 'ok' | 'error' | 'review' | 'blocked' | 'ready';
export type OnboardingRisk = 'low' | 'medium' | 'high';

export interface OnboardingDraft {
  readonly ibmiHost: string;
  readonly ibmiUser: string;
  readonly secretRefName: string;
  readonly secretRefKey: string;
  readonly schema: string;
  readonly tables: readonly string[];
  readonly journalLibrary: string;
  readonly journalName: string;
  readonly snowflakeDatabase: string;
  readonly snowflakeSchema: string;
  readonly snowflakeStage: string;
}

export interface OnboardingVerdict {
  readonly step: OnboardingStep;
  readonly status: OnboardingStatus;
  readonly errors: readonly string[];
  readonly blocked: readonly string[];
  readonly unproven: readonly string[];
  readonly proven: readonly string[];
  readonly declared: readonly string[];
  readonly nextAction: string;
  readonly risk: OnboardingRisk;
}

export interface OnboardingStepCopy {
  readonly kicker: string;
  readonly title: string;
  readonly lead: string;
  readonly primary: string;
  readonly secondary: string;
}

export type OnboardingIndexState = 'done' | 'current' | 'todo';

export type OnboardingGroupId = 'as400' | 'snowflake' | 'data';

export interface OnboardingGroup {
  readonly id: OnboardingGroupId;
  readonly label: string;
  readonly description: string;
  readonly steps: readonly OnboardingStep[];
}

export interface OnboardingIndexItem {
  readonly id: OnboardingGroupId;
  readonly index: number;
  readonly label: string;
  readonly description: string;
  readonly state: OnboardingIndexState;
  readonly entryStep: OnboardingStep;
}

export const onboardingGroupDefinitions: readonly OnboardingGroup[] = [
  {
    id: 'as400',
    label: "Your AS400",
    description: "Your data source",
    steps: ['source', 'permissions', 'journal'],
  },
  {
    id: 'snowflake',
    label: 'Snowflake',
    description: "Where your data will be stored",
    steps: ['destination'],
  },
  {
    id: 'data',
    label: "Your data",
    description: "The scope you are preparing",
    steps: ['pilot', 'verdict', 'activate'],
  },
];

export function onboardingIndex(current: OnboardingStep): readonly OnboardingIndexItem[] {
  const currentIndex = onboardingSteps.indexOf(current);
  return onboardingGroupDefinitions.map((group, index) => {
    const firstStepIndex = onboardingSteps.indexOf(group.steps[0]!);
    const lastStepIndex = onboardingSteps.indexOf(group.steps[group.steps.length - 1]!);
    return {
      id: group.id,
      index: index + 1,
      label: group.label,
      description: group.description,
      state: currentIndex < firstStepIndex ? 'todo' : currentIndex > lastStepIndex ? 'done' : 'current',
      entryStep: group.steps[0]!,
    };
  });
}

export function onboardingGroupForStep(step: OnboardingStep): OnboardingGroup {
  return onboardingGroupDefinitions.find((group) => group.steps.includes(step)) ?? onboardingGroupDefinitions[0]!;
}

const statuses = new Set<OnboardingStatus>(['ok', 'error', 'review', 'blocked', 'ready']);
const risks = new Set<OnboardingRisk>(['low', 'medium', 'high']);
const steps = new Set<OnboardingStep>(onboardingSteps);

/**
 * Brouillon pré-rempli depuis l'identité publiée par le service — aucune
 * valeur d'installation n'est codée dans l'interface. « DEMOLIB » est la
 * convention IBM i du journal rattaché à la bibliothèque des données, pas
 * une valeur de site.
 */
export function onboardingDraftFor(site: SiteIdentity): OnboardingDraft {
  return {
    ibmiHost: site.ibmiHost,
    ibmiUser: site.ibmiUser,
    secretRefName: site.secretRefName,
    secretRefKey: site.secretRefKey,
    schema: site.sourceSchema,
    // Le périmètre préparé par le produit est le manifeste complet : la
    // sélection est pré-remplie et reste modifiable, le verdict dit ensuite
    // si le choix est cohérent avec ce qui est préparé.
    tables: [...site.manifest],
    journalLibrary: 'DEMOLIB',
    journalName: site.journalName,
    snowflakeDatabase: site.destinationDatabase,
    snowflakeSchema: site.destinationSchema,
    snowflakeStage: site.snowflakeStage,
  };
}

export function onboardingStepCopy(step: OnboardingStep): OnboardingStepCopy {
  switch (step) {
    case 'source':
      return {
        kicker: "Your AS400",
        title: "Address and account",
        lead: "The specified account will be used to prepare the flow.",
        primary: "Continue",
        secondary: "Cancel",
      };
    case 'permissions':
      return {
        kicker: "Your AS400",
        title: "Access management",
        lead: "Your administrator prepares this access: nothing needs to be entered at this stage.",
        primary: "Continue",
        secondary: "Back",
      };
    case 'journal':
      return {
        kicker: "Your AS400",
        title: "Choose your data",
        lead: "The service will verify the selected library and tables.",
        primary: "Continue",
        secondary: "Back",
      };
    case 'destination':
      return {
        kicker: 'Snowflake',
        title: "Your destination",
        lead: "The development destination is configured for this flow and cannot be changed here.",
        primary: "Continue",
        secondary: "Back",
      };
    case 'pilot':
      return {
        kicker: "Your data",
        title: "Review your installation",
        lead: "The summary below will be submitted to the service for verification.",
        primary: "Continue",
        secondary: "Back",
      };
    case 'verdict':
      return {
        kicker: "Your data",
        title: "Configuration verification",
        lead: "The service reports every blocking setting and its reason.",
        primary: "Continue",
        secondary: "Back",
      };
    case 'activate':
      return {
        kicker: "Your data",
        title: "Activation unavailable",
        lead: "Nothing will start from this screen.",
        primary: "Continue",
        secondary: "Back",
      };
  }
}

export function nextOnboardingStep(step: OnboardingStep): OnboardingStep | null {
  const index = onboardingSteps.indexOf(step);
  return index >= 0 && index < onboardingSteps.length - 1 ? onboardingSteps[index + 1]! : null;
}

export function previousOnboardingStep(step: OnboardingStep): OnboardingStep | null {
  const index = onboardingSteps.indexOf(step);
  return index > 0 ? onboardingSteps[index - 1]! : null;
}

export function buildOnboardingPayload(draft: OnboardingDraft, step: OnboardingStep, site: SiteIdentity): Record<string, unknown> {
  return {
    step,
    ibmi_host: draft.ibmiHost.trim(),
    ibmi_user: draft.ibmiUser.trim(),
    tls: true,
    allow_plaintext: false,
    tls_ca_file: site.tlsCaFile,
    secret_ref_name: draft.secretRefName.trim(),
    secret_ref_key: draft.secretRefKey.trim(),
    schema: draft.schema.trim(),
    tables: [...draft.tables],
    journal_library: draft.journalLibrary.trim(),
    journal_name: draft.journalName.trim(),
    snowflake_database: draft.snowflakeDatabase.trim(),
    snowflake_schema: draft.snowflakeSchema.trim(),
    snowflake_stage: draft.snowflakeStage.trim(),
  };
}

export function parseOnboardingVerdict(payload: unknown): OnboardingVerdict {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new Error("Invalid onboarding verdict");
  }
  const value = payload as Record<string, unknown>;
  const step = value.step;
  const status = value.status;
  const risk = value.risk;
  if (value.verification_scope !== 'configuration_only' || status === 'ready'
      || !Array.isArray(value.proven) || value.proven.length !== 0 || !Array.isArray(value.declared)) {
    throw new Error("Incompatible configuration validation contract");
  }
  if (typeof step !== 'string' || !steps.has(step as OnboardingStep)) {
    throw new Error("Invalid onboarding stage");
  }
  if (typeof status !== 'string' || !statuses.has(status as OnboardingStatus)) {
    throw new Error("Invalid onboarding status");
  }
  if (typeof risk !== 'string' || !risks.has(risk as OnboardingRisk)) {
    throw new Error("Invalid onboarding risk");
  }
  if (typeof value.next_action !== 'string' || !value.next_action.trim()) {
    throw new Error("Next action missing");
  }
  return {
    step: step as OnboardingStep,
    status: status as OnboardingStatus,
    errors: stringList(value.errors),
    blocked: stringList(value.blocked),
    unproven: stringList(value.unproven),
    proven: stringList(value.proven),
    declared: stringList(value.declared),
    nextAction: value.next_action.trim(),
    risk: risk as OnboardingRisk,
  };
}

export function onboardingUnavailableCopy(reason: 'loading' | 'offline'): { readonly title: string; readonly detail: string } {
  if (reason === 'loading') {
    return {
      title: "Verification in progress",
      detail: "The service checks the consistency of the entered settings.",
    };
  }
  return {
    title: "Verification unavailable",
    detail: "The configuration service is not responding. Your input is retained: try again shortly.",
  };
}

function stringList(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
}

/** Réglages publiés par GET /v1/onboarding/defaults. Le service ne publie que
 *  des primitives techniques ; l'écran les accompagne d'un libellé métier et
 *  n'en invente aucune lorsqu'elles sont absentes. */
export interface OnboardingDefaults {
  readonly values: Readonly<Record<string, number | boolean | string>>;
  /** Identité publique du site déclaré — requise pour lire les documents de
   *  flotte et pré-remplir le formulaire. */
  readonly site: SiteIdentity;
}

export interface OnboardingDefaultRow {
  readonly key: string;
  readonly label: string;
  readonly value: string;
  readonly raw: string;
}

const ONBOARDING_DEFAULT_LABELS: Readonly<Record<string, string>> = {
  batch_entries: "Reader batch size",
  reader_timeout_seconds: "Maximum read timeout",
  poll_seconds: "Reader interval",
  max_consecutive_errors: "Tolerated consecutive errors",
  pilot_max_seconds: "Maximum pilot duration",
  pilot_max_polls: "Pilot checks",
  replica_count_at_rest: "Components active while idle",
  tls: "TLS encryption",
  allow_plaintext: "Unencrypted connection",
  tls_ca_file: "Trusted certificate",
};

export function parseOnboardingDefaults(payload: unknown): OnboardingDefaults {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new Error("Invalid published settings");
  }
  const defaults = (payload as Record<string, unknown>).defaults;
  if (typeof defaults !== 'object' || defaults === null || Array.isArray(defaults)) {
    throw new Error("Invalid published settings");
  }
  const values: Record<string, number | boolean | string> = {};
  for (const [key, value] of Object.entries(defaults)) {
    const keep =
      typeof value === 'boolean'
      || (typeof value === 'number' && Number.isFinite(value))
      || (typeof value === 'string' && value.trim().length > 0);
    if (keep) values[key] = value as number | boolean | string;
  }
  return { values, site: parseSiteIdentity((payload as Record<string, unknown>).site) };
}

export function onboardingDefaultsRows(defaults: OnboardingDefaults): readonly OnboardingDefaultRow[] {
  return Object.entries(defaults.values).map(([key, value]) => ({
    key,
    label: ONBOARDING_DEFAULT_LABELS[key] ?? key,
    value: onboardingDefaultValue(key, value),
    raw: typeof value === 'string' ? value : String(value),
  }));
}

function onboardingDefaultValue(key: string, value: number | boolean | string): string {
  if (typeof value === 'boolean') {
    if (key === 'allow_plaintext') return value ? "Allowed" : "Disallowed";
    if (key === 'tls') return value ? "Required" : "Not required";
    return value ? "Yes" : "No";
  }
  if (typeof value === 'number') {
    if (key === 'pilot_max_seconds' || key === 'reader_timeout_seconds' || key === 'poll_seconds') {
      return formatDuration(value);
    }
    if (key === 'replica_count_at_rest' && value === 0) return "None";
    return sequences(value);
  }
  return value;
}
