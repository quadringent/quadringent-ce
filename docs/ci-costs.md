# CI resource controls

CI retains seven required jobs: Python tests, Java contracts, chart validation,
three actually started images, and the product gate. Caches reuse dependencies
and identical build layers; they do not replace installation or behavior tests.

## Reuse and permissions

Release and Pages validate the newest canonical CI for the exact commit using
`scripts/check_release_ci.py`. All seven jobs on `main` must succeed.
A newer failed or active run blocks delivery. Missing CI does not automatically
launch paid work. Release and site scans remain mandatory.

Concurrency is scoped by workflow and Git ref. Obsolete work in that group can
be canceled without grouping distinct PRs or workflows together.

pip caches downloads against `pyproject.toml` and `requirements.txt`;
the current package is still installed. The Java job caches Maven dependencies
against `java/pom.xml`; `java/target` is never cached. The Python test job
keeps its JDK but has no Maven cache to save.

The three Buildx jobs use the GitHub Actions v2 cache backend with separate
image/architecture scopes. `context: .` preserves checkout inputs, pinned
bases and `COPY` invalidation. CI loads and starts images locally.

`mode=min` limits exported layers. PRs restore accessible caches without
exporting a cache for their own ref. Imports and exports are bounded to two
minutes. A cache-export failure cannot hide a build or smoke-test failure.

CI permissions are `contents: read`, including forks. It needs no package
write or OIDC permission. The official Docker action supplies cache
authentication; no custom token is passed. Automatic build records and summaries
are disabled to avoid publishing unscanned artifacts.

The scanned pytest collection report has one-day retention. Release checks use
commit/job status rather than depending on that temporary artifact.

## Cache storage

Keep the configured repository cache limit at **10 GB or less**. The workflow
does not change quotas or billing policies; `mode=min` is not a storage quota.
Read limit and usage separately with an existing authorized identity:

```sh
gh api -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/OWNER/REPO/actions/cache/storage-limit --jq .max_cache_size_gb
gh api repos/OWNER/REPO/actions/cache/usage \
  --jq '{bytes: .active_caches_size_in_bytes, count: .active_caches_count}'
```

If limit access is unavailable, inspect the repository's Actions cache settings.
Low usage does not establish the configured ceiling. Recheck before a new
repository's first run, after cache or budget changes, and before a new campaign
when the previous observation is stale. Do not enable additional paid capacity.

## Runtime budgets

| CI job | Timeout, minutes |
|---|---:|
| tests | 20 |
| image-runtime | 15 |
| verifier-runtime | 15 |
| cockpit-runtime | 15 |
| java | 10 |
| chart | 5 |
| quadringent-product-gate | 10 |

Release timeouts are 5 minutes for validation, 40 for images, 10 for chart and
10 for assembly. These bound hangs; they do not guarantee a billing ceiling.
Parallel jobs contribute separately to usage, and cancellation has a delay.

For a budgeted private qualification, track the sum of rounded runner minutes
and storage, leaving room for cancellation. Wall-clock elapsed time is not the
sum of parallel jobs. Cancellation saves remaining work only.

## Evaluate cache changes

Compare job and step timestamps, cache hits, import/export durations and rounded
minutes on an already authorized run. Retain optimizations only when they improve
the complete critical path and resource usage.

Earlier development observations varied between 15, 20 and 17 rounded minutes
for successful seven-job runs. The differences did not establish a causal cache
saving. Do not use those historical values as a prediction for this release.
Apply the account's current runner pricing, included allowance and actual SKU
when reconciling usage with billing.

Reference: [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[pip caching](https://github.com/actions/setup-python/blob/main/docs/advanced-usage.md#caching-packages),
[Maven caching](https://github.com/actions/setup-java/blob/v4/docs/advanced-usage.md#caching-packages-dependencies),
[Docker GHA cache](https://docs.docker.com/build/cache/backends/gha/) and
[GitHub cache API](https://docs.github.com/en/rest/actions/cache).
