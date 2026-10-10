# Opt-in telemetry

Telemetry helps measure community adoption and prioritize support. It is
**disabled by default**; the operator must explicitly enable it.

## Exact payload

One daily HTTPS JSON POST to the configured endpoint:

```json
{"v": 1, "install": "<SHA256 of the random installation identifier>",
 "version": "0.3.2", "tier": "community", "tables": 13, "ts": 1726670000}
```

install hashes a stable random 32-hex-character local identifier, created at
first startup in /var/lib/quadringent/telemetry-install-id with 0600 permissions.
It is not a site or customer name. version comes from chart appVersion; tier is
always community, without consulting a commercial token/status. tables is the
declared fleet catalog count; ts is the emission's Unix timestamp.

The payload contains no hosts, IP addresses, table/schema/database names,
business rows, keys, secrets, identity headers, user/IdP-group names, IBM i
journal entries or raw S3 contents.

## Operator controls

controlPlane.telemetry.enabled defaults to false. The configurable
controlPlane.telemetry.endpoint can point to the customer's own HTTPS collector;
non-HTTPS values fall back to the vendor endpoint. Emission is bounded to once
per day with a five-second timeout; failures are ignored.

The implementation is src/quadringent_control_plane/telemetry.py, using only
the standard library. This payload is independent of customer observability:
SLOs, webhook/SNS alerts, snapshots and private evidence work without telemetry.
See [observability scheduling](observability-scheduler-dev.md).
