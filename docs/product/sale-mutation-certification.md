# Bounded mutation verification

Examples use the fictional SALE table and do not prove a customer system.
Declare the actual table and key columns in site configuration and verify its
catalog before qualification.

The verifier covers C/U/D, u_before/u_after images, NULL, additive schema change
and idempotent replay. It rejects nonobject images, missing/null/nonscalar keys,
forged event identity, nonstrict ordering and multiple receivers without an
explicit chain. Reports expose no business values.

A window cut between before/after images remains incomplete. Compatible ordering
and balanced counts do not establish transaction pairing. PASS validates the
supplied raw data and local reducer; it does not replace independent IBM i and
Snowflake reconciliation.

## Offline synthetic execution

After installing the package in a development environment:

```sh
set -a
. examples/local.env
set +a
python scripts/quadringent_sale_mutation_gate.py
```

Without input, the script uses only a fixture. Exit 2 and
CONTRACT_PASS_LIVE_PENDING are expected: a synthetic contract cannot certify
an IBM i source.

For an authorized bounded real JSONL window, use private site configuration and
--input /private/path/window.jsonl; --input - reads stdin. Limits are 32 MiB and
10,000 events, not throughput or soak qualification.

| Exit | Meaning |
|---|---|
| 0 | Every requested proof is present in the window |
| 1 | Scope, key, ordering or replay violation |
| 2 | Incomplete evidence or synthetic contract only |
| 3 | Invalid file, event or configuration |

## Recovery and site qualification

python scripts/raw_checkpoint_fault_matrix.py tests local failures before
payload, manifest and checkpoint. The checkpoint cannot advance before durable
raw publication; replay reuses that raw data without duplication. It does not
prove Kubernetes restart or remote recovery.

Actual qualification requires authorized creates/updates/deletes, complete keys,
preserved NULLs, schema evolution, matching raw/destination identities, no replay
duplicates and checkpoint progress after failure. Missing evidence remains
incomplete. See [installation](install-client.md), [native qualification](native-qualification.md)
and [security boundaries](../../SECURITY.md).
