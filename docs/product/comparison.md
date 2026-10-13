# How Quadringent compares

<!-- description: A factual comparison of Quadringent with Qlik Replicate, Fivetran, Precisely Connect CDC, IBM Data Replication and Debezium for IBM i to Snowflake. -->

Quadringent copies changes from IBM i journals to Snowflake, in your own AWS or
GCP account, and shows what was read, what arrived and whether the two agree as
separate evidence. It is a DEV/INT preview and no production qualification is
claimed ([evidence](../evidence.md), [compatibility](compatibility.md)).
<!-- source: README.md, docs/product/compatibility.md, docs/product/operator-guide.md -->

Below, we put it next to the products a buyer is likely to consider for
IBM i to Snowflake. We say where those products are stronger and where
Quadringent works differently, so that you can decide whether the difference
matters to you.

## How to read this page

- Every statement about another product comes from that vendor's or project's
  public documentation, read on **2026-10-13**. The source is linked in the same
  row. Documentation changes, so check the page again before you decide.
- "According to Qlik documentation" means the statement is on the linked page.
  It is not a test. We ran none of these products.
- "Check with the vendor" means we found no statement on the pages we read.
  It does not mean the product lacks the feature.
- Pages differ in age. Each Qlik link names its documentation release in the
  address. Vendor pages for older releases may no longer apply.
- No price is given. Where a vendor publishes a pricing model, the model is
  described. Where it does not, the row says to check with the vendor.
- If you find a statement that is wrong or out of date, open an issue on the
  [project tracker](https://github.com/quadringent/quadringent-ce/issues) with
  the link to the vendor page. We correct what we can verify.

Products covered: Qlik Replicate, Fivetran (the Db2 for i connector and the
HVR documentation it builds on), Precisely (the Connect product page and the
Connect CDC support portal), IBM Data Replication (CDC Replication Engine for
Db2 for i) and the open-source Debezium connector for IBM i. Not covered:
other products that may fit an IBM i to Snowflake search, such as Striim, and
cloud-provider migration services. Their absence is not a judgment. Batch
extracts and other approaches are in
[Options to replicate AS/400 (IBM i) data to Snowflake](../articles/as400-to-snowflake-options.md).

## 1. Architecture

Where does the software run, does it need Kafka, and who operates it?

| Product | What the documentation says | Source |
|---|---|---|
| Quadringent | Self-hosted. A reader runs in a container in the Kubernetes cluster or k3s VM in your AWS or GCP account. Nothing is installed on the IBM i. No Kafka: the reader writes raw batches to your S3 or GCS bucket and loaders write to Snowflake. You operate it. | [IBM i footprint](ibm-i-footprint.md), [your data](your-data.md), [architecture](../architecture.md) |
| Qlik Replicate | According to Qlik documentation, the IBM i source needs the IBM i Access ODBC driver on the Replicate Server machine, on Windows or Linux. By default, changes are filtered on the Replicate machine with the standard `display_journal` function, which suggests nothing is installed on the IBM i for basic capture; the pages do not say so in those words. An optional component, the R4I UDTF, is installed on the IBM i server to filter changes there. Qlik also documents a cloud connector that uses a data gateway. Kafka: check with the vendor. | [Prerequisites (May 2026)](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/prereq_db2.htm), [advanced properties](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/advanced_prop_db2_source.htm), [cloud connector](https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/DataIntegration/SourcesConnections/IBM4DB2-iseries/IBM4DB2-iseries-source.htm) |
| Fivetran | According to Fivetran documentation, HVR is not installed on the Db2 for i system. It runs on a Linux or Windows machine and connects with ODBC. For the Fivetran Db2 for i connector, you install the High-Volume Agent on a server in the same network as the IBM i. A script is copied to the IBM i root directory to set the journal image option. Fivetran lists SaaS and Hybrid deployment, and Hybrid needs an Enterprise or Business Critical plan. Kafka: check with the vendor. | [HVR 6 Db2 for i as source](https://fivetran.com/docs/hvr6/requirements/source-and-target-requirements/db2-for-i-requirements/db2-for-i-as-source), [Db2 for i connector](https://fivetran.com/docs/connectors/databases/hva-db2i), [setup guide](https://fivetran.com/docs/connectors/databases/hva-db2i/setup-guide) |
| Precisely Connect CDC | According to Precisely documentation, the Connect product page says Connect detects changes through database and system logs, and lists Db2 for i among its sources and targets. The support portal, which uses the name Connect CDC, lists article titles that mention a Director, a kernel and a listener. The pages do not tie the product page and the portal together, and they do not say which components run on the IBM i or whether Kafka is needed for Snowflake: check with the vendor. | [Connect product page](https://www.precisely.com/product/precisely-connect/connect/), [support portal](https://support.precisely.com/products/connect-cdc-formerly-mimix-share/) |
| IBM Data Replication | According to IBM documentation, the CDC Replication Engine for Db2 for i is installed on the IBM i. The installing user needs several special authorities, and a `D_MIRROR` user profile is used. Where the management components run, and whether Kafka is needed: check with the vendor. | [Install the engine (11.4)](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114) |
| Debezium connector for IBM i | According to the connector README, it runs under Kafka Connect, and Kafka and Schema Registry are needed for Avro output. You operate them. The README lists IBM i object authorities for the capture user and describes no program installed on the IBM i. | [debezium-connector-ibmi README](https://github.com/debezium/debezium-connector-ibmi) |

<!-- source: java/pom.xml, pyproject.toml, docs/product/ibm-i-footprint.md, docs/product/your-data.md, docs/architecture.md -->

## 2. IBM i capture method

| Product | What the documentation says | Source |
|---|---|---|
| Quadringent | Reads journal entries over TLS. Near the journal tail it calls `QSYS2.DISPLAY_JOURNAL` with a `SELECT`. For a larger backlog it uses the journal-parsing library from the Debezium project, which calls IBM journal retrieval APIs; whether that library uses the IBM i remote command server is Not tested (see the footprint page). Tables need `*BOTH` images and a primary or unique key. One reader per journal. At most 32 tables per capture session. | [IBM i footprint](ibm-i-footprint.md#how-it-reads-the-journal), [compatibility](compatibility.md#ibm-i-source) |
| Qlik Replicate | According to Qlik documentation, all source tables of one task must be journaled to the same journal. `MINENTDTA` must be `*NONE`. Record images can be `*BOTH` or `*AFTER`; with `*AFTER`, deleted rows have null data fields. By default, change filtering happens on the Replicate machine. | [Prerequisites (May 2026)](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/prereq_db2.htm), [advanced properties](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/advanced_prop_db2_source.htm) |
| Fivetran | According to Fivetran documentation, HVR supports journal capture through the `DISPLAY_JOURNAL` table function. The journal needs `MINENTDTA(*NONE)` and `RCVSIZOPT` of `*MAXOPT3` or `*MAXOPT2`. The Db2 for i connector requires `*BOTH` images and keeping receivers at least 48 hours. Reading the journal needs `*ALLOBJ` or per-object authorities. | [HVR 6 Db2 for i as source](https://fivetran.com/docs/hvr6/requirements/source-and-target-requirements/db2-for-i-requirements/db2-for-i-as-source), [setup guide](https://fivetran.com/docs/connectors/databases/hva-db2i/setup-guide) |
| Precisely Connect CDC | According to Precisely documentation, the Connect product page says Connect detects changes through database and system logs. The support portal lists an article titled "Identifying the First Required Journal Receiver in Connect CDC for IBM i". The exact IBM i interface: check with the vendor. | [Connect product page](https://www.precisely.com/product/precisely-connect/connect/), [support portal](https://support.precisely.com/products/connect-cdc-formerly-mimix-share/) |
| IBM Data Replication | IBM's install page refers to replicating from a remote journal. The capture interface and the supported IBM i releases are not named on that page: check with the vendor. | [Install the engine (11.4)](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114) |
| Debezium connector for IBM i | According to the README, the IBM i journal is the event source and journaling needs `*BOTH` images. Limits it lists: no remote journals or failover, no CLOB or XML types, and a lost journal needs a resync. | [debezium-connector-ibmi README](https://github.com/debezium/debezium-connector-ibmi) |

<!-- source: java/src/main/java/io/quadringent/as400/JournalSession.java, docs/product/ibm-i-footprint.md, docs/product/compatibility.md -->

## 3. Snowflake delivery

| Product | What the documentation says | Source |
|---|---|---|
| Quadringent | Loaders read the raw batches from your bucket and run `MERGE` into a history table and a mirror table for each source table. Snowpipe Streaming is an optional alternative. The service user authenticates with a key pair. The default mode uses no stage, pipe or storage integration. | [Snowflake destination](snowflake-destination.md), [compatibility](compatibility.md#snowflake-prerequisites) |
| Qlik Replicate | According to Qlik documentation, the Snowflake target stages files on Snowflake, Amazon S3, Google Cloud Storage or Azure Blob Storage, and needs the Snowflake ODBC driver on the Replicate Server machine. | [Snowflake target prerequisites](https://help.qlik.com/en-US/replicate/November2025/Content/Replicate/Main/Snowflake-Target/Snowflake-target-prerequisites.htm) |
| Fivetran | According to Fivetran documentation, Snowflake is a supported destination. Each Snowflake warehouse or database needs its own destination. Fivetran adds system columns such as `_fivetran_deleted` and `_fivetran_synced`. | [Snowflake destination](https://fivetran.com/docs/destinations/snowflake), [Db2 for i connector](https://fivetran.com/docs/connectors/databases/hva-db2i) |
| Precisely Connect CDC | According to Precisely documentation, the Connect product page lists Snowflake under cloud analytics among its sources and targets. The support portal lists an article titled "Key pair authentication with Snowflake support in Connect CDC". How it stages data for Snowflake: check with the vendor. | [Connect product page](https://www.precisely.com/product/precisely-connect/connect/), [support portal](https://support.precisely.com/products/connect-cdc-formerly-mimix-share/) |
| IBM Data Replication | According to IBM documentation, the product page lists Snowflake and Kafka among targets. It does not name Db2 for i among its sources, while IBM separately documents a CDC Replication Engine for Db2 for i. That is not a statement that Db2 for i to Snowflake is unsupported: whether that engine delivers to Snowflake is unconfirmed here, so check with the vendor. | [IBM Data Replication](https://www.ibm.com/products/infosphere-data-replication), [Install the engine (11.4)](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114) |
| Debezium connector for IBM i | The README describes output through Kafka. It describes no Snowflake delivery, so a sink is a separate component: check with the project. | [debezium-connector-ibmi README](https://github.com/debezium/debezium-connector-ibmi) |

<!-- source: docs/product/snowflake-destination.md, docs/product/compatibility.md -->

## 4. Licensing model

No price figures are given here.

| Product | What the documentation says | Source |
|---|---|---|
| Quadringent | Apache-2.0. No license key, no commercial table limit, no expiry. You pay your own IBM i, cloud and Snowflake costs. No paid offering, price or service level is announced. | [Project model](../licensing.md), [LICENSE](../../LICENSE) |
| Qlik Replicate | Check with the vendor for Qlik Replicate. For the Qlik Talend Cloud connector, a different product, Qlik documents that the IBM DB2 for iSeries connector is supported with Standard and Enterprise subscriptions. | [Cloud connector](https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/DataIntegration/SourcesConnections/IBM4DB2-iseries/IBM4DB2-iseries-source.htm) |
| Fivetran | According to Fivetran documentation, pricing is usage-based, measured in Monthly Active Rows, counted per distinct primary key. Usage is tracked per connection, and connections also carry a base charge except on the Free plan. | [Usage-based pricing](https://fivetran.com/docs/usage-based-pricing) |
| Precisely Connect CDC | The pages read state no licensing terms: check with the vendor. | [Connect product page](https://www.precisely.com/product/precisely-connect/connect/) |
| IBM Data Replication | According to IBM documentation, the product page offers a no-cost trial, a live demo and a consultation. It does not state a pricing model: check with the vendor. | [IBM Data Replication](https://www.ibm.com/products/infosphere-data-replication) |
| Debezium connector for IBM i | According to the connector README, the license is Apache-2.0, and issues go to the Debezium tracker. | [debezium-connector-ibmi README](https://github.com/debezium/debezium-connector-ibmi) |

<!-- source: docs/licensing.md, LICENSE -->

## 5. Evidence shown to the operator

The question: after the software says it is running, what shows that rows
arrived in Snowflake, and that they match the source?

| Product | What the documentation says | Source |
|---|---|---|
| Quadringent | Journal capture, Snowflake delivery and destination reconciliation are separate stages with separate states. Each state is Observed, Stale, Synthetic, Historical or Not measured. A healthy verdict needs matching checkpoints and a reconciliation of the same window with zero missing, duplicate and unexpected events. These count technical events, not business rows. | [Operator guide](operator-guide.md#4-read-the-evidence), [evidence contract](quadringent-destination-proof-contract.md) |
| Qlik Replicate | Not researched for this page: we read Qlik's IBM i prerequisites and settings pages, not its monitoring or validation documentation. Check with the vendor, and ask to see a per-table check of IBM i source against Snowflake. | [Prerequisites (May 2026)](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/prereq_db2.htm) |
| Fivetran | According to Fivetran documentation, the connection Status tab shows notifications, sync history and events, and reports rows loaded per sync. That page does not say Fivetran compares source and destination row counts: check with the vendor for a per-table reconciliation. | [Connection status](https://fivetran.com/docs/using-fivetran/fivetran-dashboard/connectors/status) |
| Precisely Connect CDC | Not researched for this page: the support portal lists article titles only, and we did not open the articles. Check with the vendor for monitoring and for end-to-end delivery verification. | [Support portal](https://support.precisely.com/products/connect-cdc-formerly-mimix-share/) |
| IBM Data Replication | Not researched for this page: check with the vendor. | [IBM Data Replication](https://www.ibm.com/products/infosphere-data-replication) |
| Debezium connector for IBM i | The README mentions Prometheus metrics. Delivery to a destination is outside the connector: check with the project. | [debezium-connector-ibmi README](https://github.com/debezium/debezium-connector-ibmi) |

<!-- source: docs/product/operator-guide.md, docs/product/quadringent-destination-proof-contract.md, docs/architecture.md -->

This table is uneven. For Quadringent we know the code and the contract. For the
other products we read little, and a "not researched" row is not a finding.
Ask each vendor to show you the screen or report that answers the question
above, on your own tables.

## 6. Maturity

| Product | What the documentation says | Source |
|---|---|---|
| Quadringent | A preview, with no production reference and no service level. Qualification receipts are dated and cover only the version and scope they name. We maintain it under a pseudonymous identity; the [governance page](../governance.md) says what you can verify instead. The reader uses the journal-parsing library from the Debezium project. | [README](../../README.md), [governance](../governance.md), [support](../../SUPPORT.md), [third-party notices](../../THIRD_PARTY_NOTICES.md) |
| Qlik Replicate | Commercial product. The May 2026 support matrix lists IBM i 7.4 and 7.5 for the IBM DB2 for iSeries source, with Replicate on Windows or Red Hat Linux. Production references: check with the vendor. | [Supported source endpoints](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/Support%20Matrix/supported_source_endpoints.htm) |
| Fivetran | Commercial service. The Db2 for i connector page lists IBM i 7.2 to 7.5 and does not state a lifecycle status, so check with the vendor. | [Db2 for i connector](https://fivetran.com/docs/connectors/databases/hva-db2i) |
| Precisely Connect CDC | Commercial product. The product page says Connect and the support portal says Connect CDC (formerly MIMIX Share); the pages do not tie the two together. Supported IBM i releases: check with the vendor. | [Support portal](https://support.precisely.com/products/connect-cdc-formerly-mimix-share/) |
| IBM Data Replication | Commercial product. The 11.4 install page names no supported IBM i release: check with the vendor. | [Install the engine (11.4)](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114) |
| Debezium connector for IBM i | According to the connector README, it is incubating. Debezium announced it in release 2.6.0.Beta1 and said its documentation was still a work in progress. The stable connector index lists no separate IBM i entry. | [README](https://github.com/debezium/debezium-connector-ibmi), [2.6.0.Beta1 announcement](https://debezium.io/blog/2024/03/06/debezium-2-6-beta1-released/), [connector index](https://debezium.io/documentation/reference/stable/connectors/index.html) |

<!-- source: README.md, SUPPORT.md, docs/governance.md, THIRD_PARTY_NOTICES.md, java/pom.xml -->

## Where the alternatives are stronger

Read this before you spend time on a pilot.

| Area | What the alternatives offer that Quadringent does not |
|---|---|
| Maturity and production use | Quadringent has no production reference. Ask each vendor whether its product is used in production for IBM i to Snowflake, and for a reference on your IBM i release. |
| Support | Quadringent offers best-effort answers on public issues, with no response-time commitment ([support](../../SUPPORT.md)). Ask each vendor what support it offers and on what terms. |
| Breadth of sources and targets | According to Qlik documentation, its May 2026 support matrix has about fifty source endpoint rows, some of them variants of the same system. According to IBM documentation, its product page lists many sources and targets. According to Precisely documentation, its product page lists databases, streaming platforms and cloud analytics systems. Quadringent supports IBM i as a source and Snowflake as a destination, and nothing else. |
| IBM i capture performance options | According to Qlik documentation, an optional component on the IBM i filters changes there, for up to 300 tables per task. Quadringent has no such option and reads at most 32 tables per capture session. Throughput on your tables: Not measured for Quadringent. |
| Ecosystem | According to the Debezium connector README, the connector runs under Kafka Connect. If you already run Kafka, IBM i changes become a topic your other consumers can read. Quadringent has no such output. |
| IBM i release coverage | The only IBM i release with a recorded Quadringent measurement is 7.5, on one shared server and one table. Other releases are marked Not tested in [compatibility](compatibility.md#ibm-i-source). |

<!-- source: docs/product/compatibility.md, SUPPORT.md, docs/product/ibm-i-footprint.md -->

## Where Quadringent differs

| Difference | What it means |
|---|---|
| Open source, Apache-2.0 | You can read the reader, the loaders and the evidence code, and you can fork them. There is no license key. The license gives you rights to the versions you hold ([project model](../licensing.md)). |
| No Kafka | Raw batches go to a bucket in your account, and loaders write to Snowflake. Compared with the Debezium route, that is one less system to run and size. The pages read for the other products do not say whether they need Kafka. |
| Runs in your account | The reader, the loaders and the control plane run in your AWS or GCP account. The running software sends nothing to a project-run endpoint unless you turn on the opt-in telemetry, which is off by default ([your data](your-data.md#what-leaves-your-accounts)). |
| Nothing installed on the IBM i | The reader path runs no write statement or CL command of its own. This is not unique: Qlik basic capture also appears to need nothing on the IBM i. The backlog path calls IBM retrieval APIs through a third-party library, and whether that library uses the remote command server is Not tested. One class in the Java archive, used only for qualification runs, can change data and run `CHGJRN` ([IBM i footprint](ibm-i-footprint.md)). |
| Evidence per table | The cockpit keeps the counts apart and says Not measured when nothing was measured. In the synthetic demo, one table shows 120 events at journal capture, 118 at Snowflake delivery and destination reconciliation Not measured, all generated data ([operator guide](operator-guide.md#4-read-the-evidence)). |
| A preview with dated qualification | The qualification is small and each receipt names its version and scope. See [qualification](../qualification.md) and [evidence](../evidence.md). |

<!-- source: LICENSE, docs/licensing.md, docs/product/your-data.md, docs/product/ibm-i-footprint.md, docs/product/operator-guide.md, README.md, docs/qualification.md, docs/product/synthetic-demo.md -->

## Questions to ask any vendor, including us

1. Which IBM i releases are supported, and on which release do you have a
   customer in production?
2. What is installed on the IBM i, who owns it, and how do I remove it?
3. Which authorities does the capture profile need, and have you tested a
   profile without `*ALLOBJ`?
4. How do you behave when a journal receiver is deleted before it is read?
5. How do I see that rows reached Snowflake, per table, and that they match
   the source?
6. What happens to my data and my credentials: where are they stored, and what
   leaves my network?
7. What does it cost at my change rate, including the vendor's model, my
   cloud and Snowflake warehouse time?
8. What happens if the vendor or the maintainers stop?

## Sources

All pages were accessed on 2026-10-13.

- Qlik: [IBM DB2 for iSeries prerequisites](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/prereq_db2.htm), [advanced properties](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/advanced_prop_db2_source.htm), [cloud connector](https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/DataIntegration/SourcesConnections/IBM4DB2-iseries/IBM4DB2-iseries-source.htm), [Snowflake target prerequisites](https://help.qlik.com/en-US/replicate/November2025/Content/Replicate/Main/Snowflake-Target/Snowflake-target-prerequisites.htm), [supported source endpoints](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/Support%20Matrix/supported_source_endpoints.htm)
- Fivetran: [HVR 6 Db2 for i as source](https://fivetran.com/docs/hvr6/requirements/source-and-target-requirements/db2-for-i-requirements/db2-for-i-as-source), [Db2 for i connector](https://fivetran.com/docs/connectors/databases/hva-db2i), [setup guide](https://fivetran.com/docs/connectors/databases/hva-db2i/setup-guide), [Snowflake destination](https://fivetran.com/docs/destinations/snowflake), [usage-based pricing](https://fivetran.com/docs/usage-based-pricing), [connection status](https://fivetran.com/docs/using-fivetran/fivetran-dashboard/connectors/status)
- Precisely: [Connect product page](https://www.precisely.com/product/precisely-connect/connect/), [Connect CDC support portal](https://support.precisely.com/products/connect-cdc-formerly-mimix-share/)
- IBM: [Data Replication product page](https://www.ibm.com/products/infosphere-data-replication), [install the CDC Replication Engine for Db2 for i, 11.4](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114)
- Debezium: [debezium-connector-ibmi README](https://github.com/debezium/debezium-connector-ibmi), [release 2.6.0.Beta1](https://debezium.io/blog/2024/03/06/debezium-2-6-beta1-released/), [connector index](https://debezium.io/documentation/reference/stable/connectors/index.html)
