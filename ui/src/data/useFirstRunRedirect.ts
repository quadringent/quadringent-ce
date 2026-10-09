import { useEffect } from 'react';
import { getCockpitClient } from './useCockpit.ts';
import { href, type Route } from '../router.ts';

/** Pure : seule l'accueil (`overview`/`index`) avec zéro source déclare le
 *  premier lancement — testable sans réseau ni horloge. */
export function shouldRedirectToWizard(routeName: Route['name'], sourceCount: number): boolean {
  return (routeName === 'overview' || routeName === 'index') && sourceCount === 0;
}

/** Destination native uniquement après une lecture /v2 réussie. */
export function nativeLandingRoute(routeName: Route['name'], sourceCount: number | undefined): Route | null {
  if (sourceCount === undefined || !Number.isInteger(sourceCount) || sourceCount < 0) return null;
  if (routeName === 'setup') return { name: 'wizard', step: 'source' };
  if (routeName !== 'overview' && routeName !== 'index') return null;
  return sourceCount === 0 ? { name: 'wizard', step: 'source' } : { name: 'cockpit' };
}

/** Oriente les entrées accueil/setup vers l'installation native disponible.
 * Une erreur /v2 conserve l'écran courant. Les autres routes restent explicites. */
export function useFirstRunWizardRedirect(route: Route): void {
  useEffect(() => {
    if (!['overview', 'index', 'setup'].includes(route.name)) return;
    let alive = true;
    getCockpitClient()
      .then((client) => client.listSources())
      .then((sources) => {
        const target = nativeLandingRoute(route.name, sources.length);
        if (alive && target) window.location.hash = href(target);
      })
      .catch(() => { /* Disponibilité native inconnue : conserver la route. */ });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dépend du nom de route.
  }, [route.name]);
}
