# Costs, attribution and limits

Quadringent reports measured Snowflake credits over a 24-hour window ending at
least six hours before collection to accommodate metering delay. The window and
its age remain visible. This covers the **entire warehouse**. It is neither
exclusive pipeline attribution nor the total Snowflake bill. Do not add reports
from pipelines sharing that warehouse.

## Permission chain

The verifier reads one dedicated secure view filtered to the site's warehouse.
The control plane reads the verifier's object-storage evidence; it does not query
`ACCOUNT_USAGE` or AWS Cost Explorer. The view owner needs the permissions
required to define it. The verifier needs parent usage and `SELECT` on the view.

Adapt `infra-values/snowflake-dev-verifier-metering.sql` for owner review.
The expected view is
`<destination_database>.<destination_schema>.<destination_prefix>_<ENV>_WH_METERING`,
filtered to `<destination_prefix>_<ENV>_WH`. Print names without credentials:

```sh
python -c 'from quadringent.site_config import current; s=current(); print(s.metering_fqn, s.warehouse_name)'
```

The view publishes `START_TIME`, `END_TIME`, `WAREHOUSE_NAME`,
`CREDITS_USED_COMPUTE`, `CREDITS_USED_CLOUD_SERVICES` and `CREDITS_USED`.
Missing rows, values or permissions remain **Not measured**. An observed zero
remains zero.

## Contract price

Declare both fields or neither. The price is nonnegative decimal text with at
most eight digits before and six after the decimal point. Currency is three
uppercase letters. There is no fallback public price.

```yaml
site:
  snowflakeCreditPrice: "3.10"  # Example only; use the site's contract.
  costCurrency: EUR
```

The chart injects `QUADRINGENT_SNOWFLAKE_CREDIT_PRICE` and
`QUADRINGENT_COST_CURRENCY`; use the same variables for the CLI.
Amount is decimal `observed credits × declared price`, excluding discounts,
tax and billing adjustments. Explicit zero price differs from missing price.

In `/v1/overview`, check `snowflake_credits`,
`warehousecredits/delayed24h`,
`metering_window_<start>_<end>_<rows>` and fresh live provenance.
`pipeline.costs` exposes `warehouse`, `price_per_credit`, `currency`
and `amount`. Only admissible observations matching the site receive a current
amount. Historical, simulated or stale evidence cannot establish current costs.

Without a price, credits remain visible with **Price not declared**. Without a
measurement, show **Not measured**. Past campaign spending is not substituted.

## S3 and shared clusters

The standalone `quadringent-cost-collect` command reads infrastructure
measurements without listing business objects or modifying cloud resources.

| Source | Scope | Amount |
|---|---|---|
| CloudWatch `AWS/S3`, `BucketSizeBytes`, `StandardStorage`, daily average | Entire declared bucket | Regional storage-price estimate per month at constant volume |
| OpenCost `/allocation`, grouped by cluster and namespace, `shareIdle=false` | Quadringent namespace | Last complete UTC day's allocation |
| OpenCost `/assets` for the same window | Reported cluster nodes, volumes and management | Context total, separate from Quadringent allocation |
| Same-cluster `__idle__` allocation | Unused resources | Context already included in assets, not allocated to the namespace |

The collector fetches the regional AWS public S3 catalog on each collection;
there is no price fallback. Bytes are divided by 2³⁰ for the storage pricing
unit. Only the first Standard tier is supported; above its limit, no amount is
calculated. Preserve the measurement timestamp. S3 daily observations older
than 72 hours are rejected.

These are infrastructure estimates and allocations, not an AWS invoice.
S3 excludes requests, transfers, other storage classes, discounts and taxes.
OpenCost depends on configured cluster prices, including possible custom
fallbacks. Declare its currency explicitly; no implicit conversion occurs.
Cluster totals and idle costs are context and are not added to namespace
allocation.

Missing namespace data or pricing is not zero. Sources can fail independently.
Local evidence expires after 26 hours. Cluster windows must span exactly 24
hours and end within the last 72 hours. Reject future timestamps, historical
or simulated evidence, and mismatched site, environment, namespace, bucket
or region. Do not sum shared bucket/namespace reports or combine monthly S3
run rate, daily cluster allocation and delayed warehouse credits.

## Enable collection

Use an existing authorized AWS identity with `cloudwatch:GetMetricStatistics`,
HTTPS access to the regional catalog, and read access to the site's OpenCost
endpoint (often port 9003). Use a private endpoint or authorized port-forward.
The collector rejects URL credentials and redirects. It adds no IAM/RBAC role,
scheduler or Cost Explorer integration.

Load the actual non-production site configuration. `examples/local.env`
contains synthetic identities only. Example:

```sh
quadringent-cost-collect \
  --site-id "$QUADRINGENT_SITE_ID" --environment "$QUADRINGENT_ENVIRONMENT" \
  --namespace "$QUADRINGENT_COST_NAMESPACE" \
  --bucket "$QUADRINGENT_RAW_BUCKET" --region "$QUADRINGENT_AWS_REGION" \
  --aws-profile example-dev --opencost-url http://127.0.0.1:19003 \
  --cluster-id example-cluster --cluster-currency USD \
  --output "$PWD/.local-state/infrastructure-costs.json"
```

Omit the AWS profile for workload roles. Omit the three OpenCost options to
collect S3 alone. Exit 1 means at least one requested source was not measured;
the evidence still records available states with generic published errors.
The file is written atomically in mode `0600`; a new parent uses `0700`.

Start the control plane with:

```sh
--infrastructure-costs-source "file://$PWD/.local-state/infrastructure-costs.json"
```

It reads this file without new network calls and must already serve a live
source for the declared site and environment. A cost observation creates no
pipeline. Inspect `pipeline.infrastructure_costs` in `/v1/overview` and
the storage/cluster section in Usage.

The chart derives `QUADRINGENT_COST_NAMESPACE` from `site.namespace`.
`controlPlane.infrastructureCostsSource` can reference
`file:///var/lib/quadringent-fleet/infrastructure-costs.json`, or that filename
under the declared `fleetStateDir`, only with fleet launching and a persistent
PVC. A site-managed collector must write there under an identity readable by
the control plane. The chart does not create that collector, and the cockpit
image does not contain the standalone collection command. Schedule daily
collection within site operations, preserving other fleet state.

A future Cost Explorer integration would require its own access and API-cost
decision.
