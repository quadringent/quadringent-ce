# Community edition and project model

Quadringent's original CDC engine, API, local cockpit, chart and documentation
are licensed under [Apache-2.0](../LICENSE). Use, modification and redistribution,
including commercial redistribution, are permitted subject to that license.
Quadringent imposes no activation key, commercial table limit or expiry.
Resource constraints, access controls and required action evidence still apply.

The Community cockpit configures connections, follows evidence, explains
incidents and displays costs. Essential operating features are part of the
open-source software. IBM i, cloud and Snowflake costs are borne by the operator.

The first proposed service is a fixed-scope pilot: installation and validation
on the customer's infrastructure. Scope, acceptance criteria and deliverables
would be agreed before engagement. Later support could be offered separately.
No price, reference customer or service commitment is announced. See the
[pilot preparation guide](product/positioning-and-offer.md).

Support and operations services may be sold. A future advanced console or
hosted service could have separate terms and distinct team or multi-site
management features. No paid offer or delivery commitment exists here.
This edition requires no subscription.

Versions released under Apache-2.0 retain the rights already granted.
Users may maintain a fork or offer commercial services without publishing
their modifications or paying royalties to the author, subject to license
and third-party obligations. Apache-2.0 grants no Quadringent trademark rights.

Contributions use Apache-2.0 without copyright transfer; see
[CONTRIBUTING.md](../CONTRIBUTING.md). Third-party components retain their own
licenses: [notices and sources](../THIRD_PARTY_NOTICES.md).

## Legacy configuration compatibility

Commercial license validation and issuance modules have been removed.
`QUADRINGENT_LICENSE_KEY` and `controlPlane.licenseSecret` are unused.
An old Secret reference is not injected into pods; the chart does not delete
existing Secrets. A plaintext `controlPlane.licenseKey` remains rejected to
prevent accidental disclosure of an old token.

Telemetry remains off by default. When explicitly enabled, its `tier` field
is `community` and reads no token. Public release requires review of code,
history and artifacts; this does not change Apache-2.0 rights.
