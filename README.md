# Quadringent

**A running reader doesn't mean your data arrived.** Quadringent tracks journal reading, Snowflake delivery and reconciliation separately, and says "Not measured" instead of showing a green light it cannot back up.

We build open-source change data capture from IBM i (formerly AS/400 and iSeries) journals to Snowflake, for teams that run both themselves. It needs no Kafka and no license key, and it runs in your own AWS or GCP account. Reconciliation counts events; it does not compare business values by key.

<!-- The thesis line also opens docs/pypi.md; tests/test_pypi_metadata.py checks that they match. -->
<!-- source: docs/architecture.md, docs/licensing.md, docs/evidence.md, docs/product/your-data.md, docs/product/quadringent-destination-proof-contract.md, docs/product/synthetic-demo.md, docs/product/operator-guide.md, docs/product/try-it-local.md, site/runtime-0.3.5.json, site/recovery-0.3.4.json, site/qualification-0.3.4.json, CHANGELOG.md, pyproject.toml -->

Status: Preview, DEV/INT only. We claim no production qualification. Source version 0.4.2 needs fresh qualification; the latest observed releases are 0.3.5 (initial copy) and 0.3.4 (recovery), both 2026-10-12. Dates, tables and limits: [docs/evidence.md](docs/evidence.md).

In the 0.3.4 cold-warehouse trial, 3 of 4 targets missed our 10 s latency bar; a later run passed on all four. The IBM i CPU and I/O cost of the reader is Not measured. Only IBM i 7.5, on one shared server, has been tested, in a historical measurement on one table, and no published summary names the release.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="site/assets/cockpit-evidence-dark.png">
  <img src="site/assets/cockpit-evidence-light.png" alt="Synthetic demo of the cockpit Evidence tab for one table: journal capture 120 events, Snowflake delivery 118 events, destination reconciliation Not measured. A banner reads Synthetic demo." width="720">
</picture>

Synthetic demo, Evidence tab: 120 read, 118 delivered, reconciliation Not measured. Every value is generated.

## Try it

The synthetic demo runs the real cockpit on generated data, with no IBM i, Snowflake or cloud connection; the page blocks network requests. From a clone, with Node 24 or 26:

~~~sh
npm ci --prefix ui
node ui/scripts/build-demo.mjs --serve
~~~

Open <http://127.0.0.1:8870/#/cockpit>. The [demo guide](docs/product/synthetic-demo.md) lists what it shows.

`quadringent doctor` ([preflight and diagnosis](docs/product/operator-guide.md#2-preflight-and-diagnosis)) reads what your machine can see and changes nothing. This is real output against a placeholder host that does not resolve, with 11 of 12 checks omitted:

~~~text
Quadringent doctor: BLOCKED  (10 failed, 1 unknown, 1 skipped, 0 passed)
[UNKN] authentication.ibmi_signon [host ibmi.example.test]: IBM i sign-on was not tested; configured credentials are not proof of authentication.
[... 11 more checks omitted]
~~~

`quadringent local` copies one table on one machine with Docker ([guide](docs/product/try-it-local.md)). Both commands are listed under Unreleased in the [changelog](CHANGELOG.md), and `quadringent local` has not been validated end to end against a real IBM i and Snowflake.

The empty local control plane needs Python 3.12+ and Node 24 or 26:

~~~sh
python3 -m venv .venv
.venv/bin/python -m pip install .
npm ci --prefix ui
npm run build --prefix ui
sh scripts/start_local.sh
~~~

Open <http://127.0.0.1:8844>. It starts empty, `examples/local.env` is fictional, and nothing contacts IBM i, AWS or Snowflake. A connection created in Installation is saved as configured, not active yet, and `.local-state/` keeps it across restarts.

## Install on a real site

Read the [compatibility page](docs/product/compatibility.md) first, then follow the [installation guide](docs/product/install-default.md) or, if you manage your own values, the [Helm guide](docs/product/install-client.md). The v1 cockpit listens on loopback without authentication by default; read [Security](SECURITY.md) before exposing it.

Moving to 0.4.2 changes native capture configuration and checkpoint semantics. Keep the previous version and its state until a migration is verified ([native capture guide](docs/product/native-qualification.md)).

## Trust and verification

- [What Quadringent does on your IBM i](docs/product/ibm-i-footprint.md), start here
- [Your data: where it goes and how to leave](docs/product/your-data.md)
- [Verify a release against the public code](docs/product/verify-a-release.md)
- [Governance and continuity](docs/governance.md), if the maintainers stop
- [Support](SUPPORT.md), including what we do not offer
- [Security policy](SECURITY.md), including the response target

## Questions and background

- [FAQ](docs/product/faq.md)
- [How Quadringent compares](docs/product/comparison.md)
- [How IBM i journals work for change data capture](docs/articles/ibm-i-journals-for-cdc.md)
- [Why capture is not delivery](docs/articles/capture-delivery-reconciliation.md)
- [Options to replicate AS/400 (IBM i) data to Snowflake](docs/articles/as400-to-snowflake-options.md)

## License and the first pilot

The engine, API and cockpit are open source under [Apache-2.0](LICENSE), with no license key or commercial table limit. You pay your own infrastructure costs. Paid services or extensions may come later; rights on published versions stay in place. See the [project model](docs/licensing.md), the [dependency notices](THIRD_PARTY_NOTICES.md) and [Releases](https://github.com/quadringent/quadringent-ce/releases) for preview or stable.

We propose a fixed-scope pilot first: installation and validation on your infrastructure. No paid offering, price or SLA is announced.

### Discuss a DEV/INT pilot

Email [quadringent@proton.me](mailto:quadringent@proton.me?subject=DEV%2FINT%20pilot) to talk privately, without a GitHub account, or open a [public pilot interest request](https://github.com/quadringent/quadringent-ce/issues/new?template=pilot_interest.yml); the issue and your account name are publicly visible. Send no credentials, access, private hostnames, customer data or attachments.

We never accept a request automatically and commit to no response time. The [pilot preparation guide](docs/product/positioning-and-offer.md) lists the next steps and what to prepare.

## Documentation and contributing

[Architecture](docs/architecture.md), [API](docs/api.md), [FinOps](docs/finops.md), [operations and recovery](docs/operations.md) and [releases and delivery](docs/releasing.md). To preview the [site](site/index.html) ([visual system](docs/design-system.md)), run `npm ci --prefix ui` for fonts, then serve the root with `python3 -m http.server 8860 --bind 127.0.0.1` and open `/site/`.

To contribute, read [CONTRIBUTING.md](CONTRIBUTING.md), which lists the checks.
