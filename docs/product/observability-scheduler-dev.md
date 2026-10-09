# DEV observability scheduling

The Helm observability component refreshes an existing evidence document.
It starts no capture and cannot turn historical delivery into fresh delivery.
Qualify this runtime on the actual site before enabling it.

## Controlled activation

observability.enabled defaults to false; observability.suspend defaults to true.
Enabling the component alone therefore starts no refresh.
Load the versioned policy using:

```sh
--set-file observability.policyJson=infra-values/slo-policy-dev.json
```

Permitted schedules are */15 * * * * or 0 * * * * in UTC. Use an immutable
verifier image digest and the site's declared verifierServiceAccountName.
Concurrency is Forbid, start deadline 120 seconds, maximum runtime 600 seconds,
and no automatic Job retry after failure.

Before upgrading, retrieve effective Helm values and compare rendered manifests.
Do not blindly apply values-int.yaml, whose capture image may differ from the
installed image. Capture/cockpit Deployments must remain unchanged; this component
creates no role or secret.

## Suspension and publication

Suspending the CronJob prevents future starts but does not stop an active Job,
which remains bounded to 600 seconds. Preserve suspension in Helm values so an
upgrade cannot unexpectedly reenable it.

Exit zero proves collection/publication execution, not passing SLOs. The cockpit
shows breaches and unknown measurements. Publication uses the previous ETag:
never force a conflict with newer evidence. If publication status is unknown,
reread the current S3 version before a manual retry.

Forbid applies only to Jobs managed by this CronJob, not a manually created Job.
A single idle check cannot prevent the next scheduled start. For manual publication:

1. Retain effective values and the original suspension setting.
2. Suspend through a controlled DEV change, verifying unchanged capture/cockpit manifests.
3. Wait for existing observer Jobs to terminate; do not introduce a second evidence writer.
4. Run the bounded manual Job, retain its result and verify the S3 version.
   Do not force/repeat a PUT when publication is uncertain.
5. Restore the original setting and observe the next scheduled cycle.

ETag protection remains required because new ingestion evidence can arrive during
collection. Preserve newer evidence rather than overwrite it with an older archive.

Logs retain the publication receipt (version/ETag). The full SLO report lives in
the published S3 version; /work/observation.json is ephemeral. This component sends
no external notification and reports no Snowflake cost without the required
metering view.
