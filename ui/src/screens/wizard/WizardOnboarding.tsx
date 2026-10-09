import { useEffect, useState } from 'react';
import type { WizardStep } from '../../router.ts';
import { href } from '../../router.ts';
import { createWizardClient } from '../../data/wizardClient.ts';
import type { ControlPlaneV2Client, SourceRecord, DestinationRecord } from '../../data/controlPlaneV2Client.ts';
import { WizardActivate } from './WizardActivate.tsx';
import { WizardSource } from './WizardSource.tsx';
import { WizardSnowflake } from './WizardSnowflake.tsx';
import { WizardTables } from './WizardTables.tsx';

/** L'activation précède la session : aucune lecture protégée à cette étape. */
export function shouldLoadExistingSource(step: WizardStep): boolean {
  return step !== 'activate';
}

/** L'API renvoie les sources par date de création croissante. */
export function selectResumeSource<T>(sources: readonly T[]): T | null {
  return sources.at(-1) ?? null;
}

/** Une sélection inconnue ne se replie jamais sur une autre destination. */
export function selectResumeDestination<T extends { readonly id: string }>(destinations: readonly T[], selectedId: string | null = null): T | null {
  if (selectedId) return destinations.find((item) => item.id === selectedId) ?? null;
  return destinations.length === 1 ? destinations[0]! : null;
}

/** Le callback fournit des IDs de tables, pas les IDs de pipelines du cockpit. */
export function startedTablesHref(tableIds: readonly string[]): string | null {
  if (tableIds.length === 0) return null;
  return href({ name: 'cockpit' });
}

/**
 * Orchestrateur de l'assistant v2 : un client `/v2` partagé et la navigation
 * entre les quatre écrans du premier lancement. Le client par défaut est
 * chargé de façon paresseuse par `createWizardClient()` (démo en développement,
 * réel sinon — voir `wizardClient.ts`) ; un `client` injecté prend le pas
 * dessus, ce qui garde cet écran testable en rendu serveur synchrone, sans
 * jamais référencer les fixtures depuis ce module.
 *
 * Aucun identifiant de source n'est fixé en dur : `GET /v2/sources` retrouve
 * une source déjà créée pour cette installation (reprise après rechargement,
 * `docs/api-v2.md` §« Enveloppe d'action générique ») ; `WizardSource`
 * notifie l'identifiant réel dès qu'il existe (création ou réutilisation)
 * via `onSourceReady`, conservé ici pour l'écran « Tables » suivant.
 */
export function WizardOnboarding({
  step,
  activationToken = null,
  client: injectedClient,
}: {
  readonly step: WizardStep;
  /** Jeton du lien d'activation à usage unique (`?token=...`) — lu par l'appelant
   *  depuis `window.location.hash`, jamais depuis ce composant, pour rester
   *  testable côté serveur sans DOM. */
  readonly activationToken?: string | null;
  /** Injection pour les tests ; en production, laissé vide pour charger le
   *  client par défaut de façon paresseuse. */
  readonly client?: ControlPlaneV2Client;
}) {
  const [client, setClient] = useState<ControlPlaneV2Client | null>(injectedClient ?? null);
  const [destinations, setDestinations] = useState<readonly DestinationRecord[]>([]);
  const [destination, setDestination] = useState<DestinationRecord | null>(null);
  const [confirmedDestinationId, setConfirmedDestinationId] = useState<string | null>(null);
  const [destinationLoading, setDestinationLoading] = useState(false);
  const [destinationLoadError, setDestinationLoadError] = useState(false);
  const [destinationReload, setDestinationReload] = useState(0);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [existingSource, setExistingSource] = useState<SourceRecord | null>(null);

  useEffect(() => {
    if (injectedClient) return;
    let cancelled = false;
    void createWizardClient().then((created) => { if (!cancelled) setClient(created); });
    return () => { cancelled = true; };
  }, [injectedClient]);

  // Reprise après rechargement : retrouve une source déjà créée pour cette
  // installation (`GET /v2/sources`) — jamais son mot de passe, jamais
  // persisté côté client. Si un nouvel identifiant a été créé pour changer
  // les paramètres, reprendre le plus récent. Ne bloque
  // jamais le premier rendu (SSR synchrone, tests d'écran) : l'écran
  // « Source » se réaffiche simplement une fois la réponse arrivée.
  useEffect(() => {
    if (!client || !shouldLoadExistingSource(step)) return;
    let cancelled = false;
    void client.listSources()
      .then((sources) => {
        if (cancelled) return;
        const latest = selectResumeSource(sources);
        setExistingSource(latest);
        if (latest) setSourceId((current) => current ?? latest.id);
      })
      .catch(() => { /* pas de source existante ou service indisponible — reprise à zéro */ });
    return () => { cancelled = true; };
  }, [client, step]);

  // L'identifiant dans le lien est une sélection, jamais une preuve d'accès.
  // La liste authentifiée de cette installation valide sa portée après reload.
  useEffect(() => {
    if (!client || !shouldLoadExistingSource(step)) return;
    let cancelled = false;
    setDestinationLoading(true);
    setDestinationLoadError(false);
    void client.listDestinations().then((items) => {
      if (cancelled) return;
      setDestinations(items);
      const selectedId = typeof window === 'undefined' ? null
        : new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('destination_id');
      const selected = selectResumeDestination(items, selectedId);
      if (selectedId && !selected) setDestinationLoadError(true);
      // Préserver le reçu unique en mémoire quand l'écran est quitté puis repris.
      setDestination((current) => current && selected?.id === current.id
        ? current : selected);
    }).catch(() => { if (!cancelled) setDestinationLoadError(true); })
      .finally(() => { if (!cancelled) setDestinationLoading(false); });
    return () => { cancelled = true; };
  }, [client, step, destinationReload]);

  const chooseDestination = (selected: DestinationRecord) => {
    setDestination(selected);
    if (destination?.id !== selected.id) setConfirmedDestinationId(null);
    setDestinations((items) => items.some((item) => item.id === selected.id) ? items : [...items, selected]);
    if (typeof window !== 'undefined') {
      const path = window.location.hash.split('?')[0];
      window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${path}?destination_id=${encodeURIComponent(selected.id)}`);
    }
  };

  const goto = (next: WizardStep) => {
    if (typeof window !== 'undefined') window.location.hash = href({ name: 'wizard', step: next }) + (destination ? `?destination_id=${encodeURIComponent(destination.id)}` : '');
  };

  if (!client) {
    return (
      <div className="wizard">
        <p role="status">Preparing the wizard…</p>
      </div>
    );
  }

  switch (step) {
    case 'activate':
      return <WizardActivate client={client} token={activationToken} onActivated={() => goto('source')} />;
    case 'source':
      return (
        <WizardSource
          client={client}
          existingSource={existingSource}
          onSourceReady={setSourceId}
          onBack={() => { if (typeof window !== 'undefined') window.location.hash = href({ name: 'cockpit' }); }}
          onContinue={() => goto('snowflake')}
        />
      );
    case 'snowflake':
      return (
        <WizardSnowflake
          client={client}
          existingDestination={destination}
          destinations={destinations}
          loading={destinationLoading}
          loadError={destinationLoadError}
          onRetry={() => setDestinationReload((value) => value + 1)}
          onDestinationReady={chooseDestination}
          onBack={() => goto('source')}
          onContinue={(id) => { setConfirmedDestinationId(id); goto('tables'); }}
        />
      );
    case 'tables':
      return (
        <WizardTables
          client={client}
          sourceId={sourceId ?? existingSource?.id ?? null}
          destinationId={confirmedDestinationId === destination?.id ? destination.id : null}
          onBack={() => goto('snowflake')}
          onStarted={(tableIds) => {
            const target = startedTablesHref(tableIds);
            if (target && typeof window !== 'undefined') {
              window.location.hash = target;
            }
          }}
        />
      );
  }
  return null;
}
