# Quadringent

**Open-source change data capture from IBM i journals to Snowflake.** It is a
DEV/INT preview and no production qualification is claimed. It needs no Kafka and
no license key, runs in your own AWS or GCP account, and shows separate evidence
for reading, delivery to Snowflake and reconciliation of events: with evidence, or
marked "Not measured".

Quadringent copies changes from IBM i (AS/400) journals to Snowflake and records
evidence at each stage: journal reading, raw storage, loading and reconciliation.
It runs against your own IBM i and Snowflake. Active capture does not guarantee
that the destination is up to date. The cockpit keeps these states apart and shows
**Not measured** when evidence is missing.

**Community edition, DEV/INT preview.** The engine, API and local cockpit are
open source under Apache-2.0, with no license key or commercial table limit.
Operators pay their own infrastructure costs. No production qualification is
claimed. Release availability and preview or stable status are listed on the
[releases page](https://github.com/quadringent/quadringent-ce/releases).

This package contains the `quadringent` installer command and the control plane.
The Helm chart and Terraform modules are in the source distribution, not in the
wheel.

## Where to start

- [Questions for a first call (FAQ)](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/faq.md)
- [How Quadringent compares with other options](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/comparison.md)
- [What Quadringent does on your IBM i](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/ibm-i-footprint.md)
- [Your data: where it goes and how to leave](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/your-data.md)
- [Verify a release](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/verify-a-release.md)
- [Compatibility and prerequisites](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/compatibility.md)
- [Install on AWS or GCP](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/install-default.md)
- [Try the cockpit with synthetic data](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/synthetic-demo.md)
- [Governance and continuity](https://github.com/quadringent/quadringent-ce/blob/main/docs/governance.md)
- [Support](https://github.com/quadringent/quadringent-ce/blob/main/SUPPORT.md)
- [Security policy](https://github.com/quadringent/quadringent-ce/blob/main/SECURITY.md)

## Articles

- [How IBM i journals work for change data capture](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/ibm-i-journals-for-cdc.md)
- [Capture is not delivery: proving IBM i data reached Snowflake](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/capture-delivery-reconciliation.md)
- [Options to replicate AS/400 (IBM i) data to Snowflake](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/as400-to-snowflake-options.md)

Full documentation: [README](https://github.com/quadringent/quadringent-ce/blob/main/README.md).
Changes: [changelog](https://github.com/quadringent/quadringent-ce/blob/main/CHANGELOG.md).

<!-- source: README.md, docs/product/faq.md, docs/product/comparison.md, docs/articles/ibm-i-journals-for-cdc.md -->
<!-- source: docs/product/install-default.md -->
