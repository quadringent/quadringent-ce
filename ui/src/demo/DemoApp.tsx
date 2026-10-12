import { useEffect } from 'react';
import { useRoute } from '../router.ts';
import { CockpitApp } from '../screens/cockpit/CockpitApp.tsx';
import { WizardOnboarding } from '../screens/wizard/WizardOnboarding.tsx';
import { SyntheticDemoBanner } from '../components/SyntheticDemoBanner.tsx';
import { SYNTHETIC_DEMO_COPY } from '../domain/operator.ts';
import { demoRoute, type SyntheticDemoProvenance } from '../domain/syntheticDemo.ts';

/** Synthetic demo shell: the real cockpit and wizard screens over in-memory
 *  demo clients, under a persistent banner. v1 screens are not rendered. */
export function DemoApp({ provenance }: { readonly provenance: SyntheticDemoProvenance }) {
  const { route, unavailable } = demoRoute(useRoute());

  useEffect(() => {
    if (route.name === 'cockpit') document.title = `Quadringent · Cockpit · ${SYNTHETIC_DEMO_COPY.title}`;
  }, [route.name]);

  return (
    <div className="synthetic-demo">
      <SyntheticDemoBanner provenance={provenance} />
      {unavailable ? (
        <p className="synthetic-demo__notice" role="status">
          {SYNTHETIC_DEMO_COPY.unavailable} <a href="#/cockpit">{SYNTHETIC_DEMO_COPY.backHome}</a>
        </p>
      ) : null}
      {route.name === 'wizard'
        ? <WizardOnboarding step={route.step} activationToken={null} />
        : <CockpitApp route={route} />}
    </div>
  );
}
