# Positioning and pilot preparation

## What a team can evaluate

Quadringent Community connects an IBM i journal to Snowflake and makes reading,
raw storage, loading and reconciliation visible. The engine, API and cockpit
are open source under [Apache-2.0](../../LICENSE). The team operates its own
infrastructure and retains control of access and data.

Historical DEV/INT qualifications cover AWS VM, EKS, GCP VM and GKE depending
on version and scenario. The [README](../../README.md) separates measurements,
limitations and simulations. They provide no SLA, PROD qualification or
admission of future artifacts. Check the current version and its dated
qualification in the README; earlier evidence remains bound to its original
artifacts. Check Releases for actual availability.

## First proposed service

A **fixed-scope pilot: installation and validation on the customer's
infrastructure**, with usable evidence and a runbook. The software remains open
source; a service would cover preparation, installation and validation work.
Later support could be agreed separately. Managed hosting would be considered
only after a real need is confirmed.

This offer is not available for purchase. No price, contractual lead time,
reference customer or SLA is announced. This guide prepares a decision without
a sales form, automatic outreach or a fictional sales contact.

## Describe an anonymized scenario

Open a [pilot interest request](https://github.com/quadringent/quadringent-ce/issues/new?template=pilot_interest.yml)
to describe your intended use and success criteria. GitHub sign-in is required.
State whether IBM i, Snowflake and a DEV/INT environment are available, and
which technical role would operate the evaluation. Include no credentials,
private hostnames, customer names or business data. Interest is the first step;
scope, scheduling and support still need a separate agreement.

## Prepare the scope

Complete this worksheet privately. Put no secrets or personal identifiers in
a public issue.

| Decision | Information to prepare |
|---|---|
| Objective | Mirror use, operations to observe, target delay to measure |
| Tables | Two representative tables initially; types, key, volume and change frequency |
| Source | IBM i version, journaling, TLS access, read permissions and retention policy |
| Destination | Dedicated database and schema, scoped Snowflake role, approved authentication |
| Infrastructure | AWS or GCP, VM or cluster, RAW and checkpoint storage, budget and resource owner |
| Access | Temporary identities, key management, expiry, cleanup and authorized internal contacts |
| Acceptance | Typed data, retained events, no duplicates, latency and recovery scenario |
| Deliverables | Dated receipts, observed limitations, runbook and responsibilities after the pilot |

IBM i, cloud and Snowflake costs are borne by the operator. Define budget and
stop rules before installation. PROD access is not implicit; a first pilot
runs in DEV/INT.

## Proposed path

1. **Prerequisite review.** Select the admitted version and scope; verify
   permissions, TLS, journaling, destination and retention. See
   [installation](install-default.md) and [security](../../SECURITY.md).
2. **Installation.** Deploy verified public artifacts on the chosen site.
   Retain effective settings and resource identities.
3. **Copy and continuity.** Validate the initial copy and approved changes,
   comparing source/history/mirror with dated measurements.
4. **Bounded recovery.** Agree the outage scenario, authorization and required
   evidence before injecting a fault.
5. **Acceptance.** State what is verified, partial or absent; deliver the
   runbook, then remove temporary access as agreed.

A Ready pod, reachable API or accepted action is insufficient to accept
replication. Acceptance concerns actual delivered data. One site's results
do not become a universal guarantee.

## Presentation direction

The static page helps a reader understand IBM i → RAW and checkpoint →
Snowflake, then install Community. Pilot preparation is the second path, with
a concrete guide. Presentation follows the cockpit
[design system](../design-system.md): IBM Plex, blue for activity, green for
observed measurements, amber for limitations and demonstrations. Cockpit
screenshots are labeled synthetic. Figures follow the product and remain
bound to a version, targets and observation window.

Three hierarchy references informed the presentation:
[Debezium](https://debezium.io/documentation/reference/stable/) introduces
capture before connectors; [Temporal](https://docs.temporal.io/) proceeds from
concept to getting started and deployment; [Airbyte](https://docs.airbyte.com/)
separates replication from other paths. These references inform reading order
without claiming equivalent capabilities or scope. The page includes no
fictional prices, customer logos, simulated live states or availability claims.
