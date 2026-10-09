# Quadringent chart values reference

Generated from `chart/values.schema.json` by `scripts/generate_chart_values_doc.py` — do not edit manually.

| Key | Type | Required (root) | Description |
|---|---|---|---|
| `gcpIdentityMode` | `string` | no | GKE: Workload Identity annotation; dedicated Compute Engine VM: node metadata identity without a GKE annotation. |
| `image` | `object` | yes | Main container image (journal reader). |
| `image.repository` | `string` | yes | Image registry/repository, e.g. ghcr.io/quadringent/quadringent. |
| `image.digest` | `string` | no | Image digest; empty permitted when the chart default applies. |
| `image.pullPolicy` | `string` | yes | Container image pull policy. |
| `image.pullSecret` | `string` | no | Kubernetes imagePullSecret name for a private registry. Empty: no imagePullSecret mounted. |
| `deployment` | `object` | yes | Deployment tier. The chart is nonproduction: no PROD profile accepted. |
| `deployment.environment` | `string` | yes | Declared nonproduction environment. No PROD promotion path. |
| `deployment.productionPromotionAllowed` | `boolean` | yes | Must remain false; rendering otherwise fails. |
| `site` | `object` | yes | Declared site identity injected into the -site ConfigMap (QUADRINGENT_*). No default: a release without site declaration is refused. |
| `site.connectionDeclared` | `boolean` | no | false: no IBM i connection declared yet (default installer before the wizard); IBM i/Snowflake connection fields stay empty and are not required. true (default): connection fields are required. |
| `site.snowflakeCreditPrice` | `string | number` | no | String or number: accepts textual or numeric --set. |
| `site.costCurrency` | `string` | no | Price currency (e.g. EUR, USD). Declare with snowflakeCreditPrice. |
| `site.id` | `string` | no | Short site identifier (lowercase letters, digits, hyphens). |
| `site.awsAccountId` | `string` | no | Twelve-digit AWS account. Required for storage.backend=aws; empty for storage.backend=gcs. |
| `site.namespace` | `string` | no | Declared Kubernetes namespace: rendering fails for any other namespace. Empty: deployment.environment/siteGuards requires an explicit declaration with a dedicated message. |
| `site.ibmiHost` | `string` | no | IBM i partition host or IP. |
| `site.ibmiUser` | `string` | no | IBM i reader user. |
| `site.sourceSchema` | `string` | no | IBM i source schema (library). |
| `site.proofTable` | `string` | no | Proof-path table: must belong to fleetTables. |
| `site.journalName` | `string` | no | Observed IBM i journal name. |
| `site.rawBucket` | `string` | no | Site raw S3/GCS bucket. |
| `site.rawPrefixRoot` | `string` | no | Object prefix root, e.g. <product>/<library>. |
| `site.checkpointTable` | `string` | no | DynamoDB checkpoint table. |
| `site.destinationDatabase` | `string` | no | Snowflake destination database. |
| `site.destinationSchema` | `string` | no | Snowflake destination schema. |
| `site.destinationId` | `string` | no | Snowflake destination identifier. |
| `site.destinationPrefix` | `string` | no | Provisioned Snowflake object prefix. Empty: runtime uses QUADRINGENT. |
| `site.destinationMode` | `string` | no | Mirror/history path. Empty or copy_merge: COPY INTO + MERGE. streaming: Snowpipe Streaming + loader-driven mirror MERGE (requires streaming.profileSecret). |
| `site.snowflakeAccount` | `string` | no | Site Snowflake account. |
| `site.snowflakeConnection` | `string` | no | Optional Snowflake connection alias. |
| `site.snowflakeWarehouse` | `string` | no | Existing Snowflake warehouse, never created or suspended by the chart. Empty: dedicated warehouse derived from the prefix. |
| `site.awsProfile` | `string` | no | Optional local AWS profile. |
| `site.autonomousProofName` | `string` | no | Autonomous proof document name. Empty: runtime uses quadringent-autonomous-latest.json. |
| `site.fleetTables` | `array<string>` | no | Fleet table manifest. |
| `site.keyedTables` | `array<string>` | no | Manifest tables with a business key. |
| `site.provisionedStages` | `array<string>` | no | Already provisioned Snowflake stages. |
| `site.reservableTables` | `array<string>` | no | Reservable tables beyond the proof table. Empty by default. |
| `site.proofKeyColumns` | `array<string>` | no | Proof-path key columns. |
| `site.forbiddenFragments` | `array<string>` | no | Forbidden fragments in generated identifiers (collision guard). |
| `nameOverride` | `string` | no | Override chart short name. |
| `fullnameOverride` | `string` | no | Override full resource names. |
| `serviceAccount` | `object` | yes | Capture ServiceAccount (journal reader and v2 pipeline Jobs/Deployments; site S3/GCS/DynamoDB access). |
| `serviceAccount.create` | `boolean` | no | true (default): chart creates the ServiceAccount and cloud identity annotation according to storage.backend. false: provided outside the chart. |
| `serviceAccount.name` | `string` | no | Kubernetes name (DNS-1123 label): lowercase letters, digits and hyphens, up to 63 characters. |
| `serviceAccount.roleArn` | `string` | no | AWS IAM role ARN (IRSA) associated with the ServiceAccount. Required and exclusive of gcpServiceAccount when create=true and storage.backend=aws. |
| `serviceAccount.gcpServiceAccount` | `string` | no | Expected GCP account, bound through GKE Workload Identity or attached to the VM. Required and exclusive of roleArn when create=true and storage.backend=gcs. |
| `serviceAccount.annotations` | `object` | no | Additional ServiceAccount annotations, merged with the chart-managed cloud annotation (eks.amazonaws.com/role-arn or iam.gke.io/gcp-service-account). |
| `replicaCount` | `integer` | yes | Only 0 or 1: IBM i does not tolerate concurrent journal readers. |
| `pilot` | `object` | no | Bounded certification tier: one Job runs the worker with a strict budget; permanent Deployment remains at zero. |
| `pilot.enabled` | `boolean` | no | Enable the bounded pilot Job. |
| `pilot.runId` | `string` | no | Fresh identifier reserved by the worker before source access. |
| `pilot.maxPolls` | `integer` | no | Strictly positive integer. |
| `pilot.maxSeconds` | `integer` | no | Strictly positive integer. |
| `pilot.shutdownMarginSeconds` | `integer` | no | Nonnegative integer. |
| `pilot.ttlSecondsAfterFinished` | `integer` | no | Nonnegative integer. |
| `pilot.proofWindow` | `object` | no | Pilot proof window. |
| `pilot.proofWindow.enabled` | `boolean` | no | Enable the proof window. |
| `pilot.proofWindow.id` | `string` | no | Proof window identifier. |
| `pilot.proofWindow.seconds` | `integer` | no | Strictly positive integer. |
| `pilot.proofWindow.count` | `integer` | no | Strictly positive integer. |
| `verifierServiceAccountName` | `string` | no | Verification workload ServiceAccount (observability CronJob and verifier Job), created outside the chart with its dedicated IAM role. |
| `verification` | `object` | no | Site proof-path observer. Creates no role or permission. |
| `verification.enabled` | `boolean` | no | Enable the verifier Job. |
| `verification.publishProof` | `boolean` | no | Publish verified evidence. |
| `verification.slo` | `object` | no |  |
| `verification.slo.enabled` | `boolean` | no | Enable SLO verification. |
| `verification.slo.policyJson` | `string` | no | Versioned SLO policy loaded with --set-file. |
| `verification.imageDigest` | `string` | no | Image digest; empty permitted when the chart default applies. |
| `observability` | `object` | no | Independent refresh of existing evidence; never starts capture. |
| `observability.enabled` | `boolean` | no | Enable the observability CronJob. |
| `observability.suspend` | `boolean` | no | Suspend the CronJob without deleting it. |
| `observability.schedule` | `string` | no | Collection cron expression. |
| `observability.policyJson` | `string` | no | Embedded SLO policy. |
| `observability.imageDigest` | `string` | no | Image digest; empty permitted when the chart default applies. |
| `observability.alerts` | `object` | no | Optional SLO alerts. Without a declared destination, the CronJob measures silently. |
| `observability.alerts.webhookUrl` | `string` | no | Optional alert webhook. |
| `observability.alerts.snsTopic` | `string` | no | Optional alert SNS topic. |
| `fleetObserve` | `object` | no | Periodic fleet Snowflake delivery measurement. |
| `fleetObserve.enabled` | `boolean` | no | Enable the fleet-observe CronJob. |
| `fleetObserve.suspend` | `boolean` | no | Suspend the CronJob without deleting it. |
| `fleetObserve.schedule` | `string` | no | Collection cron expression. |
| `fleetObserve.imageDigest` | `string` | no | Image digest; empty permitted when the chart default applies. |
| `fleetObserve.manifestBudget` | `integer` | no | Strictly positive integer. |
| `safety` | `object` | no |  |
| `safety.maxConsecutiveErrors` | `integer` | no | Never more than 3: each error is another IBM i sign-on. |
| `bootstrap` | `object` | no | Explicit reader bootstrap. |
| `bootstrap.mode` | `string` | no | checkpoint requires durable state; tail reads a finite window; explicit requires receiver + sequence. |
| `bootstrap.receiver` | `string` | no | Starting receiver (explicit mode). |
| `bootstrap.sequence` | `string` | no | Starting sequence (explicit mode). |
| `nodeSelector` | `object` | no | Kubernetes node selector for the site's cluster. |
| `tolerations` | `array<object>` | no | Standard Kubernetes tolerations (passed through to the manifest). |
| `podAntiAffinity` | `object` | no | Declared anti-affinity against other site-cluster workloads. |
| `podAntiAffinity.enabled` | `boolean` | no | Enable anti-affinity. |
| `podAntiAffinity.namespaces` | `array<string>` | no | Namespaces targeted by anti-affinity. |
| `podAntiAffinity.matchLabels` | `object` | no | Extensible Kubernetes label/selector map (arbitrary key/value). |
| `affinity` | `object` | no | Raw Kubernetes affinity/nodeAffinity/podAffinity rule, passed through. Expert alternative to podAntiAffinity. |
| `resources` | `object` | yes | Kubernetes container resource requests and limits. |
| `resources.requests` | `object` | no |  |
| `resources.requests.cpu` | `string` | no | Requested CPU, e.g. 50m or "1". |
| `resources.requests.memory` | `string` | no | Requested memory, e.g. 512Mi. |
| `resources.limits` | `object` | no |  |
| `resources.limits.cpu` | `string` | no | Maximum CPU, e.g. "1". |
| `resources.limits.memory` | `string` | no | Maximum memory, e.g. 2Gi. |
| `podSecurityContext` | `object` | no | Pod-level securityContext, separate from the chart-managed container securityContext. Empty by default: behavior unchanged. |
| `imagePullSecrets` | `array<object>` | no | Additional imagePullSecrets beyond image.pullSecret for multiple registries. |
| `podAnnotations` | `object` | no | Additional reader Pod annotations. |
| `podLabels` | `object` | no | Additional reader Pod labels. |
| `extraEnv` | `array<object>` | no | Additional reader container environment variables (Kubernetes EnvVar format). |
| `extraVolumes` | `array<object>` | no | Additional reader Pod volumes (Kubernetes Volume format). |
| `extraVolumeMounts` | `array<object>` | no | Additional reader container mounts (Kubernetes VolumeMount format). |
| `networkPolicy` | `object` | no | Optional namespace NetworkPolicy. Disabled by default; existing network behavior unchanged. |
| `networkPolicy.enabled` | `boolean` | no | Enable a basic NetworkPolicy. |
| `ibmi` | `object` | yes | Reader runtime configuration. |
| `ibmi.host` | `string` | no | IBM i host. |
| `ibmi.user` | `string` | no | IBM i user. |
| `ibmi.schema` | `string` | no | Source schema (library). |
| `ibmi.table` | `string` | no | Observed table. |
| `ibmi.journalLibrary` | `string` | no | Journal library. |
| `ibmi.journalName` | `string` | no | Journal name. |
| `ibmi.sourceTimeZone` | `string` | no | Explicit IANA zone verified against IBM i at connection. Never inferred as UTC. |
| `ibmi.passwordSecret` | `object` | no | Secret containing the IBM i password. Never in values. |
| `ibmi.passwordSecret.name` | `string` | no |  |
| `ibmi.passwordSecret.key` | `string` | no |  |
| `as400` | `object` | yes | TLS settings and JTOpen ports. |
| `as400.tls` | `boolean` | no | Must remain true: IBM i plaintext is forbidden. |
| `as400.allowPlaintext` | `boolean` | no | Must remain false. |
| `as400.tlsCaFile` | `string` | no | Absolute TLS authority path without directory traversal. |
| `as400.tlsCaSecret` | `object` | no |  |
| `as400.tlsCaSecret.name` | `string` | no |  |
| `as400.tlsCaSecret.key` | `string` | no |  |
| `as400.databasePort` | `string | integer` | no | JTOpen database port override. Empty: driver default. |
| `as400.signonPort` | `string | integer` | no | JTOpen sign-on port override. Empty: driver default. |
| `as400.commandPort` | `string | integer` | no | JTOpen command port override. Empty: driver default. |
| `consoleSnapshot` | `object` | no |  |
| `consoleSnapshot.enabled` | `boolean` | no | Enable console snapshot publication. |
| `consoleSnapshot.s3Key` | `string` | no | Console snapshot object key. |
| `consoleSnapshot.intervalSeconds` | `number` | no | Publication interval in seconds. Must be > 0. |
| `streaming` | `object` | no | Snowpipe Streaming profile, used only when site.destinationMode=streaming. |
| `streaming.profileSecret` | `object` | no | Secret containing the Snowpipe SDK profile.json. Never in values. |
| `streaming.profileSecret.name` | `string` | no |  |
| `streaming.profileSecret.key` | `string` | no |  |
| `streaming.mountPath` | `string` | no | Secret volume mount path containing profile.json. |
| `controlPlane` | `object` | no | Read-only observation API, independent of the IBM i reader. |
| `controlPlane.enabled` | `boolean` | no |  |
| `controlPlane.licenseKey` | `string` | no | Obsolete: nonempty values refused at render (no commercial license required). Empty tolerated for compatibility. |
| `controlPlane.licenseSecret` | `object` | no | Legacy license Secret reference, tolerated but never injected into the pod. |
| `controlPlane.licenseSecret.name` | `string` | no |  |
| `controlPlane.licenseSecret.key` | `string` | no |  |
| `controlPlane.image` | `object` | no |  |
| `controlPlane.image.repository` | `string` | no |  |
| `controlPlane.image.digest` | `string` | no | Image digest; empty permitted when the chart default applies. |
| `controlPlane.image.pullPolicy` | `string` | no | Container image pull policy. |
| `controlPlane.image.allowedRepositories` | `array<string | null>` | no | Allowed control plane image registries (immutable digest always required). An entry may be null when --set populates an isolated index. |
| `controlPlane.replicaCount` | `integer` | no | Always 1: this version's read-only control plane is not sized for multiple replicas. |
| `controlPlane.source` | `string` | no | Control plane live-read source. |
| `controlPlane.windowProof` | `string` | no | Optional immutable destination-window evidence. |
| `controlPlane.infrastructureCostsSource` | `string` | no | Infrastructure cost collection source (separate file). |
| `controlPlane.port` | `integer` | no | Control plane listening port. |
| `controlPlane.refreshSeconds` | `number` | no | Live-read refresh interval. |
| `controlPlane.catalogRefreshSeconds` | `integer` | no | Nonnegative integer. |
| `controlPlane.progressionSeconds` | `integer` | no | Strictly positive integer. |
| `controlPlane.host` | `string` | no | Listening address. Loopback only without authentication. |
| `controlPlane.auth` | `object` | no | Authentication delegated to the site's IdP through a trusted proxy. |
| `controlPlane.auth.enabled` | `boolean` | no |  |
| `controlPlane.auth.userHeader` | `string` | no | HTTP user identity header set by the proxy. |
| `controlPlane.auth.groupsHeader` | `string` | no | HTTP group header set by the proxy. |
| `controlPlane.auth.operatorGroups` | `string | array` | no | Single string or string list (accepts --set scalar and list forms). |
| `controlPlane.auth.adminGroups` | `string | array` | no | Single string or string list (accepts --set scalar and list forms). |
| `controlPlane.auth.proxySecret` | `object` | no | Shared Secret attesting passage through the proxy. |
| `controlPlane.auth.proxySecret.secretName` | `string` | no |  |
| `controlPlane.auth.proxySecret.secretKey` | `string` | no |  |
| `controlPlane.telemetry` | `object` | no | Opt-in product telemetry, disabled by default. |
| `controlPlane.telemetry.enabled` | `boolean` | no |  |
| `controlPlane.telemetry.endpoint` | `string` | no | Empty: vendor endpoint; otherwise HTTPS required. |
| `controlPlane.serviceAccount` | `object` | no |  |
| `controlPlane.serviceAccount.create` | `boolean` | no |  |
| `controlPlane.serviceAccount.name` | `string` | no | Kubernetes name (DNS-1123 label): lowercase letters, digits and hyphens, up to 63 characters. |
| `controlPlane.serviceAccount.roleArn` | `string` | no | AWS IAM role ARN (IRSA) associated with the ServiceAccount. Required and exclusive of gcpServiceAccount when storage.backend=aws. |
| `controlPlane.serviceAccount.gcpServiceAccount` | `string` | no | Expected GCP account, bound through GKE Workload Identity or attached to the VM. Required and exclusive of roleArn when storage.backend=gcs. |
| `controlPlane.serviceAccount.annotations` | `object` | no | Additional ServiceAccount annotations, merged with the chart-managed cloud annotation (eks.amazonaws.com/role-arn or iam.gke.io/gcp-service-account). |
| `controlPlane.launch` | `object` | no | Controller-driven capture Job launch. Disabled by default. |
| `controlPlane.launch.enabled` | `boolean` | no |  |
| `controlPlane.launch.jobTemplate` | `string` | no | Job JSON template loaded with --set-file. |
| `controlPlane.launch.fleetCatalog` | `string` | no | Fleet JSON catalog loaded with --set-file. |
| `controlPlane.launch.fleetSidecar` | `string` | no | Fleet JSON sidecar loaded with --set-file. |
| `controlPlane.launch.fleetConsoleSource` | `string` | no | Optional fleet console source, derived by default from storage.rawBucket/site.rawPrefixRoot. |
| `controlPlane.fleetState` | `object` | no |  |
| `controlPlane.fleetState.persistence` | `object` | no |  |
| `controlPlane.fleetState.persistence.enabled` | `boolean` | no | true: persistent PVC. false: volatile emptyDir. |
| `controlPlane.fleetState.persistence.size` | `string` | no | PVC size, e.g. 256Mi. |
| `controlPlane.fleetState.persistence.storageClassName` | `string` | no | "": default StorageClass; "-": dynamic provisioning disabled. |
| `controlPlane.resources` | `object` | no | Kubernetes container resource requests and limits. |
| `controlPlane.resources.requests` | `object` | no |  |
| `controlPlane.resources.requests.cpu` | `string` | no | Requested CPU, e.g. 50m or "1". |
| `controlPlane.resources.requests.memory` | `string` | no | Requested memory, e.g. 512Mi. |
| `controlPlane.resources.limits` | `object` | no |  |
| `controlPlane.resources.limits.cpu` | `string` | no | Maximum CPU, e.g. "1". |
| `controlPlane.resources.limits.memory` | `string` | no | Maximum memory, e.g. 2Gi. |
| `controlPlane.v2` | `object` | no | v2 surface (FastAPI/uvicorn, PostgreSQL), a second container beside v1 in the same Pod. Disabled by default. |
| `controlPlane.v2.enabled` | `boolean` | no |  |
| `controlPlane.v2.image` | `object` | no |  |
| `controlPlane.v2.image.repository` | `string` | no | Empty: controlPlane.image.repository. |
| `controlPlane.v2.image.digest` | `string` | no | Image digest; empty permitted when the chart default applies. |
| `controlPlane.v2.port` | `integer` | no | Strictly positive integer. |
| `controlPlane.v2.orgId` | `string` | no | v2 organization identifier; empty: site.id. |
| `controlPlane.v2.readerPollSeconds` | `number | string` | no | Strictly positive number (integer or decimal). String form also accepted: --set converts only integers automatically; decimals such as 15.1 remain strings. |
| `controlPlane.v2.loaderPollSeconds` | `number | string` | no | Strictly positive number (integer or decimal). String form also accepted: --set converts only integers automatically; decimals such as 15.1 remain strings. |
| `controlPlane.v2.loaderHistoryMode` | `string` | no | Snowpipe Streaming or synchronous SQL MERGE for small CDC flows. |
| `controlPlane.v2.loaderFlushEachBatch` | `boolean` | no | Force Snowpipe flush after each batch to reduce latency; may increase costs and request frequency. |
| `controlPlane.v2.resources` | `object` | no | Kubernetes container resource requests and limits. |
| `controlPlane.v2.resources.requests` | `object` | no |  |
| `controlPlane.v2.resources.requests.cpu` | `string` | no | Requested CPU, e.g. 50m or "1". |
| `controlPlane.v2.resources.requests.memory` | `string` | no | Requested memory, e.g. 512Mi. |
| `controlPlane.v2.resources.limits` | `object` | no |  |
| `controlPlane.v2.resources.limits.cpu` | `string` | no | Maximum CPU, e.g. "1". |
| `controlPlane.v2.resources.limits.memory` | `string` | no | Maximum memory, e.g. 2Gi. |
| `postgres` | `object` | no | Embedded PostgreSQL for v2 control plane state. Enabled by default; externalDatabase replaces it in expert mode. |
| `postgres.runAsUser` | `integer` | no | Image postgres user UID (70 on Alpine, 999 on Debian), required by runAsNonRoot. |
| `postgres.enabled` | `boolean` | no |  |
| `postgres.image` | `object` | no |  |
| `postgres.image.repository` | `string` | no |  |
| `postgres.image.digest` | `string` | no | Immutable image digest (sha256:<64 hex>). Never a mutable tag. |
| `postgres.image.pullPolicy` | `string` | no | Container image pull policy. |
| `postgres.database` | `string` | no | PostgreSQL database name. |
| `postgres.username` | `string` | no | PostgreSQL application user. |
| `postgres.port` | `integer` | no | Strictly positive integer. |
| `postgres.storage` | `object` | no |  |
| `postgres.storage.size` | `string` | no | PostgreSQL volume size, e.g. 5Gi. |
| `postgres.storage.storageClassName` | `string` | no | "": cluster default StorageClass. |
| `postgres.resources` | `object` | no | Kubernetes container resource requests and limits. |
| `postgres.resources.requests` | `object` | no |  |
| `postgres.resources.requests.cpu` | `string` | no | Requested CPU, e.g. 50m or "1". |
| `postgres.resources.requests.memory` | `string` | no | Requested memory, e.g. 512Mi. |
| `postgres.resources.limits` | `object` | no |  |
| `postgres.resources.limits.cpu` | `string` | no | Maximum CPU, e.g. "1". |
| `postgres.resources.limits.memory` | `string` | no | Maximum memory, e.g. 2Gi. |
| `postgres.backup` | `object` | no | Periodic logical pg_dump backup to site object storage. Disabled by default. |
| `postgres.backup.enabled` | `boolean` | no |  |
| `postgres.backup.schedule` | `string` | no | Cron expression (UTC). |
| `postgres.backup.destinationPrefix` | `string` | no | Backup object prefix under storage.rawBucket. |
| `postgres.backup.serviceAccountName` | `string` | no | Empty: site's default capture identity. |
| `externalDatabase` | `object` | no | External managed PostgreSQL, mutually exclusive alternative to postgres. |
| `externalDatabase.url` | `string` | no | Full SQLAlchemy DSN (postgresql+psycopg://...). Alternative to existingSecret. |
| `externalDatabase.existingSecret` | `string` | no | Site Secret containing the DSN under existingSecretKey. |
| `externalDatabase.existingSecretKey` | `string` | no |  |
| `preflight` | `object` | no | Bounded preflight diagnostic. Disabled by default. |
| `preflight.enabled` | `boolean` | no |  |
| `preflight.serviceAccountName` | `string` | no | Preflight Pod ServiceAccount. |
| `preflight.snowflakeOidc` | `boolean` | no | Mount a projected OIDC token and enable the in-cluster Snowflake check. |
| `preflight.image` | `object` | no |  |
| `preflight.image.repository` | `string` | no | Default: controlPlane.image.repository. |
| `preflight.image.digest` | `string` | no | Image digest; empty permitted when the chart default applies. |
| `tuning` | `object` | yes | Reader tuning verified by the 2026-08-27 soak. Remeasure before changing it. |
| `tuning.batchEntries` | `integer` | yes | Strictly positive integer. |
| `tuning.maxDecodedEntries` | `integer` | yes | Strictly positive integer. |
| `tuning.journalBufferSize` | `integer` | yes | Strictly positive integer. |
| `tuning.readerTimeoutSeconds` | `number | string` | yes | Strictly positive number (integer or decimal). String form also accepted: --set converts only integers automatically; decimals such as 15.1 remain strings. |
| `tuning.retrieveTimeoutMs` | `integer` | yes | Strictly positive integer. |
| `tuning.moreDataTimeoutMs` | `integer` | yes | Strictly positive integer. |
| `tuning.finalPositionTimeoutMs` | `integer` | yes | Strictly positive integer. |
| `tuning.pollSeconds` | `integer` | yes | Strictly positive integer. |
| `tuning.emptyProbe` | `boolean` | yes | Conditional probe: only when lag <= window. |
| `tuning.minPollSeconds` | `number | string` | yes | Strictly positive number (integer or decimal). String form also accepted: --set converts only integers automatically; decimals such as 15.1 remain strings. |
| `tuning.tailProbe` | `boolean` | yes | Bounded ATTACHED receiver probe each poll: a new entry is visible without rereading the full catalog. |
| `tuning.catalogQueryTimeoutSeconds` | `integer` | yes | Strictly positive integer. |
| `tuning.receiverMetadataLimit` | `integer` | yes | Strictly positive integer. |
| `tuning.catalogCachePolls` | `integer` | yes | Strictly positive integer. |
| `tuning.catalogCacheSeconds` | `integer` | yes | Strictly positive integer. |
| `tuning.catalogMaxStale` | `integer` | yes | Strictly positive integer. |
| `tuning.catchUpBatchEntries` | `integer` | yes | Strictly positive integer. |
| `tuning.catchUpDivisor` | `integer` | yes | Strictly positive integer. |
| `tuning.metricsIntervalSeconds` | `integer` | yes | Strictly positive integer. |
| `storage` | `object` | yes |  |
| `storage.backend` | `string` | no | Durable storage backend: aws (S3+DynamoDB) or gcs (one GCS bucket). |
| `storage.rawBucket` | `string` | no | Raw S3/GCS bucket. |
| `storage.rawPrefix` | `string` | no | Observed table object prefix. |
| `storage.streamKey` | `string` | no | Checkpoint stream key. |
| `storage.checkpointTable` | `string` | no | DynamoDB checkpoint table (aws backend). |
| `storage.checkpointBucket` | `string` | no | GCS checkpoint bucket (gcs backend). |
| `aws` | `object` | yes |  |
| `aws.region` | `string` | no | Site AWS region, e.g. eu-west-3. Required when storage.backend=aws. |
| `costGuardrail` | `object` | no | Indicative S3 threshold for an external alert; the chart creates no alarm or budget. |
| `costGuardrail.enabled` | `boolean` | no | Display the indicative threshold in AWS Helm notes. |
| `costGuardrail.requestAlarmThresholdPerDay` | `integer` | no | Strictly positive integer. |
