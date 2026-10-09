# Infrastructure examples

All identities are synthetic. Example digests illustrate format; they do not
prove image availability. These files describe no real deployment and must
not be applied unchanged.

Copy only necessary files into the site's private configuration and adapt
account, bucket, scoped roles/trusts, prefixes, Snowflake objects, secret
references, verified CA and admitted image digests.

The chart accepts dev/int/test/staging, at most one reader, and a Helm namespace
exactly matching `site.namespace`. Examples use dev and quadringent-demo.
Production promotion remains refused. The infrastructure owner reviews scoped
policies before application. The control plane has no billing-account access.

See [installation](../docs/product/install-client.md),
[FinOps](../docs/finops.md) and [security](../SECURITY.md).

## Synthetic catalog and sidecar

`fleet-catalog-int.json` is a static offline Helm fixture.
Its name is not an observed environment. ID columns, zero volumes, DEMORECV,
sequence 1 and date 2000-01-01 are synthetic; uncertain continuity prevents
real qualification.

`fleet-sidecar-int.json` is generated from that catalog only.
It has zero fixture volumes, unobserved cost/progress, uncertain continuity and
blocked certification. Its metadata-only technical source/observability
envelopes use historical; they refer to the fixed-date fixture, not customer
observations. Structured pipeline provenance is
`quality.evidence_kind: simulation`, and source state remains unknown.

`tests/test_public_samples.py` regenerates the document at a fixed date,
adds only the compatible explanatory annotations and compares exact payload.
Never use these files as runtime observation or release admission.
A real site produces its own private catalog.
