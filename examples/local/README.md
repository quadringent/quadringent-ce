# Local mode example

Fictitious values only: host names under `example.test`, documentation
addresses in `192.0.2.0/24`, no secret.

`config.sample.json` shows what `quadringent local init` writes to
`~/.quadringent/local/<name>/config.json`. You do not edit it by hand: run

```sh
quadringent local init --name sales \
  --ibmi-host ibmi.example.test --ibmi-user COPYUSER \
  --table SALESLIB/ORDERS --snowflake-account myorg-myaccount
```

For an IBM i reached by address on an isolated network, TLS stays the default.
If it must be plain text, the settings that change are:

```sh
quadringent local init --name lab \
  --ibmi-host 192.0.2.10 --ibmi-user COPYUSER --table SALESLIB/ORDERS \
  --snowflake-account myorg-myaccount \
  --allow-plaintext-ibmi --source-time-zone Europe/Paris
```

The IBM i password and the Snowflake key are never in this file: they are 0600
files in `secrets/`. See [Try Quadringent on one machine](../../docs/product/try-it-local.md)
for the steps, the costs, the limits and how to clean up.
