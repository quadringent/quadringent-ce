# Quadringent

**A running reader doesn't mean your data arrived.** Quadringent tracks journal reading, Snowflake delivery and reconciliation separately, and says "Not measured" instead of showing a green light it cannot back up.

We build open-source change data capture from IBM i (formerly AS/400 and iSeries) journals to Snowflake, for teams that run both themselves. It needs no Kafka and no license key, and it runs in your own AWS or GCP account. The engine, API and cockpit are Apache-2.0, with no commercial table limit. You pay your own infrastructure costs. Reconciliation counts events; it does not compare business values by key.

Version 0.4 · Apache-2.0. Initial copy of synthetic tables tested against a real IBM i and a real Snowflake account on four cloud targets (release 0.3.5, 2026-10-12), plus reader-kill recovery with source writes on 0.3.4. Receipt summaries are public. Limits (latency, IBM i CPU and I/O cost, IBM i 7.5 as the only release tested) and figures: [evidence page](https://github.com/quadringent/quadringent-ce/blob/main/docs/evidence.md).

Start with a proof of concept on a non-production IBM i. The reader installs nothing on the IBM i, your data stays in your own cloud and Snowflake accounts, and `quadringent uninstall` removes the Helm release. We help with pilots: see the [installation guide](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/install-default.md) and the [README](https://github.com/quadringent/quadringent-ce/blob/main/README.md).

This package holds the `quadringent` installer command and the control plane. The Helm chart and Terraform modules are in the source distribution, not the wheel.

## Try it

The synthetic demo runs the real cockpit on generated data, with no IBM i, Snowflake or cloud connection. It needs Git and Node 24 or 26.

~~~sh
git clone https://github.com/quadringent/quadringent-ce.git
cd quadringent-ce
npm ci --prefix ui
node ui/scripts/build-demo.mjs --serve
~~~

Open <http://127.0.0.1:8870/#/cockpit>. A banner shows the source version and commit; `PROVENANCE.json` records them. The [demo guide](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/synthetic-demo.md) lists what it shows. Read the [compatibility page](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/compatibility.md) before you install on a real site.

## Read next

- [What Quadringent does on your IBM i](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/ibm-i-footprint.md)
- [Your data: where it goes and how to leave](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/your-data.md)
- [Verify a release](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/verify-a-release.md)
- [Install on AWS or GCP](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/install-default.md)
- [FAQ](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/faq.md)
- [How Quadringent compares](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/comparison.md)
- [Governance and continuity](https://github.com/quadringent/quadringent-ce/blob/main/docs/governance.md)
- [Support](https://github.com/quadringent/quadringent-ce/blob/main/SUPPORT.md) and [security policy](https://github.com/quadringent/quadringent-ce/blob/main/SECURITY.md)
- [How IBM i journals work for change data capture](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/ibm-i-journals-for-cdc.md)
- [Capture is not delivery](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/capture-delivery-reconciliation.md)
- [Options to replicate AS/400 (IBM i) data to Snowflake](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/as400-to-snowflake-options.md)
- [README](https://github.com/quadringent/quadringent-ce/blob/main/README.md), [changelog](https://github.com/quadringent/quadringent-ce/blob/main/CHANGELOG.md), [releases](https://github.com/quadringent/quadringent-ce/releases)

<!-- The thesis line also opens README.md; tests/test_pypi_metadata.py checks that they match. -->
<!-- source: README.md, docs/evidence.md, docs/product/faq.md, docs/product/comparison.md, docs/articles/ibm-i-journals-for-cdc.md -->
<!-- source: docs/product/install-default.md, docs/product/synthetic-demo.md -->
