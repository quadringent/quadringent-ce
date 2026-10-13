# Quadringent

**A running reader doesn't mean your data arrived.** Quadringent tracks journal reading, Snowflake delivery and reconciliation separately, and says "Not measured" instead of showing a green light it cannot back up.

We build open-source change data capture from IBM i (formerly AS/400 and iSeries) journals to Snowflake. It needs no Kafka and no license key, and it runs in your own AWS or GCP account. For each table the cockpit shows what the journal reader saw, what reached Snowflake and whether the two were reconciled, and it marks a stage Not measured when we have no evidence for it. Reconciliation counts events; it does not compare business values by key. The engine, API and cockpit are Apache-2.0, with no license key or commercial table limit. You pay your own infrastructure costs.

Status: Preview, DEV/INT only. We claim no production qualification. Dates, tables and limits are in the [evidence page](https://github.com/quadringent/quadringent-ce/blob/main/docs/evidence.md). In the 0.3.4 cold-warehouse trial, 3 of 4 targets missed our 10 s latency bar; a later run passed on all four. The IBM i CPU and I/O cost of the reader is Not measured. Only IBM i 7.5, on one shared server, has been tested, in a historical measurement on one table, and no published summary names the release.

This package contains the `quadringent` installer command and the control plane. The Helm chart and Terraform modules are in the source distribution, not in the wheel.

## Try it

The synthetic demo runs the real cockpit on generated data, with no IBM i, Snowflake or cloud connection. It needs Git and Node 24 or 26.

~~~sh
git clone https://github.com/quadringent/quadringent-ce.git
cd quadringent-ce
npm ci --prefix ui
node ui/scripts/build-demo.mjs --serve
~~~

Open <http://127.0.0.1:8870/#/cockpit>. A banner shows the source version and commit, and `PROVENANCE.json` in the build records the same facts. Then read the [compatibility page](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/compatibility.md) before you install on a real site.

## Read next

- [What Quadringent does on your IBM i](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/ibm-i-footprint.md)
- [Your data: where it goes and how to leave](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/your-data.md)
- [Verify a release](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/verify-a-release.md)
- [Install on AWS or GCP](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/install-default.md)
- [Try the cockpit with synthetic data](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/synthetic-demo.md)
- [Questions for a first call (FAQ)](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/faq.md)
- [How Quadringent compares with other options](https://github.com/quadringent/quadringent-ce/blob/main/docs/product/comparison.md)
- [Governance and continuity](https://github.com/quadringent/quadringent-ce/blob/main/docs/governance.md)
- [Support](https://github.com/quadringent/quadringent-ce/blob/main/SUPPORT.md)
- [Security policy](https://github.com/quadringent/quadringent-ce/blob/main/SECURITY.md)

For background, read [how IBM i journals work for change data capture](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/ibm-i-journals-for-cdc.md), [capture is not delivery: proving IBM i data reached Snowflake](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/capture-delivery-reconciliation.md) and [options to replicate AS/400 (IBM i) data to Snowflake](https://github.com/quadringent/quadringent-ce/blob/main/docs/articles/as400-to-snowflake-options.md).

The full [README](https://github.com/quadringent/quadringent-ce/blob/main/README.md) and the [changelog](https://github.com/quadringent/quadringent-ce/blob/main/CHANGELOG.md) are on GitHub. Release availability, and whether a release is preview or stable, is listed on the [releases page](https://github.com/quadringent/quadringent-ce/releases).

<!-- The opening paragraph is also the opening of README.md; edit both together. tests/test_pypi_metadata.py checks that they match. -->
<!-- source: README.md, docs/evidence.md, docs/product/faq.md, docs/product/comparison.md, docs/articles/ibm-i-journals-for-cdc.md -->
<!-- source: docs/product/install-default.md, docs/product/synthetic-demo.md -->
