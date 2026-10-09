# Site TLS trust

Images contain no customer certificate. Supply the CA bundle verified by the
IBM i administrator through a Kubernetes Secret containing public PEM
certificates only, never a private key. Declare
`as400.tlsCaSecret.name`, `as400.tlsCaSecret.key` and `as400.tlsCaFile`.

The chart mounts it read-only. `TlsTrust` loads it on connection and rejects
missing files, private keys and invalid chains.

For renewal, verify fingerprints independently, publish a transition bundle
containing both authorities, update the Secret reference, and perform a
controlled restart after checking the durable checkpoint. Running JVMs must
restart to reload trust.
