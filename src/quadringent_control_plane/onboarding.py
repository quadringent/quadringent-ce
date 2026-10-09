"""Pure onboarding evaluation. No IBM i, S3, or Snowflake I/O."""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from quadringent.site_config import SiteConfig, current as _current_site

# Périmètres déclarés du site — résolus à l'accès via ``__getattr__``, jamais
# figés à une installation : le manifeste de la flotte, les zones de dépôt
# réellement provisionnées et les tables dotées d'une clé unique. Une table
# sans zone provisionnée n'a pas de destination : le verdict le dit au lieu de
# le supposer.
FLEET_TABLES: tuple[str, ...]
PROVISIONED_STAGES: tuple[str, ...]
KEYED_TABLES: tuple[str, ...]
DEFAULTS: dict[str, object]
STAGE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")


def _site() -> SiteConfig:
    return _current_site()


def _provisioned_stage_names(site: SiteConfig) -> tuple[str, ...]:
    """Noms de zones provisionnées, dérivés of the tables déclarées par le site."""

    return tuple(site.snowflake_stage_for(name) for name in site.provisioned_stages)


def _defaults(site: SiteConfig) -> dict[str, object]:
    """Défauts produit — le chemin du bundle CA appartient au site déclaré."""

    return {
        "batch_entries": 60000,
        "reader_timeout_seconds": 15,
        "poll_seconds": 2,
        "max_consecutive_errors": 3,
        "pilot_max_seconds": 600,
        "pilot_max_polls": 200,
        "replica_count_at_rest": 0,
        "tls": True,
        "allow_plaintext": False,
        "tls_ca_file": site.tls_ca_file,
    }


def _site_identity(site: SiteConfig) -> dict[str, object]:
    """Identité publique du site déclaré, publiée pour l'interface.

    Aucune donnée sensible n'est exposée : le mot de passe n'est jamais
    publié — seuls le nom et la clé de la référence Kubernetes le sont, pour
    pré-remplir le formulaire sans recopie manuelle. Chaque valeur provient
    de la configuration du site, jamais du code.
    """

    return {
        "site_id": site.site_id,
        "fleet_id": site.fleet_id,
        "environment": site.fleet_environment,
        "runtime_environment": site.environment,
        "destination_database": site.destination_database,
        "destination_schema": site.destination_schema,
        "destination_namespace": site.destination_namespace,
        "source_schema": site.source_schema,
        "journal_name": site.journal_name,
        "tables": list(site.fleet_tables),
        "proof_table": site.proof_table,
        "runtime_pipeline_id": site.site_id,
        "ibmi_host": site.ibmi_host,
        "ibmi_user": site.ibmi_user,
        "tls_ca_file": site.tls_ca_file,
        "secret_ref_name": site.ibmi_password_secret or "",
        "secret_ref_key": site.ibmi_password_key or "",
        "snowflake_stage": site.proof_stage,
    }


def __getattr__(name: str) -> object:
    site_attributes = {
        "FLEET_TABLES": lambda site: site.fleet_tables,
        "PROVISIONED_STAGES": _provisioned_stage_names,
        "KEYED_TABLES": lambda site: site.keyed_tables,
        "DEFAULTS": _defaults,
        "SITE_IDENTITY": _site_identity,
    }
    resolver = site_attributes.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver(_site())

STEPS: tuple[str, ...] = (
    "source",
    "permissions",
    "journal",
    "destination",
    "pilot",
    "verdict",
    "activate",
)

_SECRET_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "privatekey",
    "credential",
    "credentials",
    "api_key",
    "apikey",
}

def stage_for_table(table: str) -> str:
    """Zone de dépôt correspondant à une table, par convention de nommage."""

    return _site().snowflake_stage_for(table)


def parse_table_selection(value: object) -> tuple[tuple[str, ...], list[str]]:
    """Valide une sélection de tables, sans jamais renvoyer les valeurs reçues.

    Une liste est exigée : une chaîne unique serait ambiguë. Chaque motif de
    refus est signalé une seule fois, pour rester lisible dans le verdict.
    """

    if value is None:
        return (), ["Select tables to copy"]
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return (), ["Unreadable table selection"]
    selection: list[str] = []
    unreadable = False
    unknown = False
    duplicate = False
    for item in value:
        if not isinstance(item, str) or not item.strip():
            unreadable = True
            continue
        table = item.strip().upper()
        if table not in _site().fleet_tables:
            unknown = True
            continue
        if table in selection:
            duplicate = True
            continue
        selection.append(table)
    errors: list[str] = []
    if unreadable:
        errors.append("Unreadable table selection")
    if unknown:
        errors.append("At least one configured table is outside the prepared scope")
    if duplicate:
        errors.append("The selection contains a duplicate table")
    if not errors and not selection:
        errors.append("Select tables to copy")
    return tuple(selection), errors


def evaluate_onboarding(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate one onboarding payload and return the operator verdict.

    The function never echoes secrets. A secret-like key is a hard error.
    """

    secret_keys = _secret_keys(payload)
    if secret_keys:
        return _result(
            step="source",
            status="error",
            blocked=["A secret must never be sent to the control plane"],
            next_action="Use a Kubernetes Secret reference instead of a credential value",
            risk="high",
        )

    step = payload.get("step", "source")
    if step not in STEPS:
        return _result(
            step="source",
            status="error",
            blocked=["Unknown onboarding step"],
            next_action="Start again from the IBM i connection",
            risk="medium",
        )

    errors: list[str] = []
    blocked: list[str] = []
    unproven: list[str] = []
    declared: list[str] = []

    site = _site()

    tls = payload.get("tls", True)
    allow_plaintext = payload.get("allow_plaintext", False)
    if tls is not True or allow_plaintext is True:
        blocked.append("TLS is required; plaintext IBM i connections are not allowed")
    tls_ca_file = _text(payload.get("tls_ca_file", _defaults(site)["tls_ca_file"]))
    if tls is True and not tls_ca_file:
        blocked.append("An IBM i TLS CA file is required")

    host = _text(payload.get("ibmi_host"))
    user = _text(payload.get("ibmi_user"))
    secret_name = _text(payload.get("secret_ref_name"))
    secret_key = _text(payload.get("secret_ref_key"))
    schema = _text(payload.get("schema"))
    table = _text(payload.get("table"))
    journal_library = _text(payload.get("journal_library"))
    journal_name = _text(payload.get("journal_name"))
    database = _text(payload.get("snowflake_database"))
    snowflake_schema = _text(payload.get("snowflake_schema"))
    stage = _text(payload.get("snowflake_stage"))

    if step in STEPS[STEPS.index("source") :]:
        if not host:
            errors.append("An IBM i host is required")
        if not user:
            errors.append("An IBM i user is required")
        if not errors and not blocked and step != "source":
            declared.append("IBM i source identity entered")

    if STEPS.index(step) >= STEPS.index("permissions"):
        if not secret_name or not secret_key:
            errors.append("A Kubernetes secret reference is required")
        connectivity = payload.get("connectivity")
        # Client assertions are not runtime evidence, including an explicit "ok".
        unproven.append("IBM i connectivity not observed")
        if connectivity == "error":
            blocked.append("Connectivity failure declared; no server verification")

    selection: tuple[str, ...] = ()
    selection_declared = False
    if STEPS.index(step) >= STEPS.index("journal"):
        if schema != site.source_schema:
            errors.append(f"The journal for this site is {site.source_schema}")
        declared_selection = payload.get("tables")
        selection_declared = declared_selection is not None
        if declared_selection is None and table:
            # Compatibilité : une charge utile mono-table reste acceptable.
            declared_selection = [table]
        selection, selection_errors = parse_table_selection(declared_selection)
        errors.extend(selection_errors)
        if not journal_library or not journal_name:
            errors.append("An IBM i journal is required")
        elif not errors and selection:
            declared.append(
                f"Journal {site.source_schema} selected for {len(selection)} table(s)"
            )
        unproven.append("IBM i journaling and permissions unverified")

    if STEPS.index(step) >= STEPS.index("destination"):
        if database != site.destination_database or snowflake_schema != site.destination_schema:
            blocked.append(
                f"The Snowflake destination must remain {site.destination_namespace}"
            )
        provisioned = _provisioned_stage_names(site)
        if stage not in provisioned:
            blocked.append(
                "This stage is not provisioned in "
                f"{site.fleet_environment}"
            )
        destination_text = f"{database}.{snowflake_schema}.{stage}".upper()
        if any(fragment.upper() in destination_text for fragment in site.forbidden_fragments):
            blocked.append("Destination disallowed by site policy")
        missing_destinations = [
            name for name in selection if site.snowflake_stage_for(name) not in provisioned
        ]
        if missing_destinations:
            blocked.append(
                "Snowflake destination not provisioned for "
                f"{len(missing_destinations)} of the {len(selection)} selected tables"
            )
        if not blocked and STEPS.index(step) > STEPS.index("destination"):
            declared.append(f"Snowflake destination {site.fleet_environment} selected")
        unproven.append("This form does not verify the Snowflake destination")
        # Une table sans cle n'empeche pas la copie, mais elle prive du tableau
        # d'etat : le dire ici evite de le decouvrir apres la copie.
        keyed = [name for name in selection if name in site.keyed_tables]
        unkeyed = [name for name in selection if name not in site.keyed_tables]
        if selection:
            declared.append(
                f"Grouping key declared for {len(keyed)} of the "
                f"{len(selection)} selected tables"
            )
        if unkeyed:
            unproven.append(
                f"{len(unkeyed)} table(s) without a business key: identified by their "
                "physical position (RRN) — a file reorganization "
                "(RGZPFM, CLRPFM) will require resynchronization"
            )

    if STEPS.index(step) >= STEPS.index("pilot"):
        declared.append("Proposed pilot configuration: bounded budget, zero backoff")
        unproven.append("This form does not verify pilot execution or safeguards")
        if payload.get("pilot") == "fail":
            blocked.append("Pilot failure declared; no server verification")

    if step == "activate" and selection_declared and selection and selection != site.fleet_tables:
        blocked.append(
            "Activation requires selecting the "
            f"{len(site.fleet_tables)} prepared tables; {len(selection)} declared"
        )

    if step in {"verdict", "activate"}:
        if blocked or errors:
            status = "error"
            next_action = "Resolve the blocker before any pilot"
            risk = "high"
        else:
            status = "blocked" if step == "activate" else "review"
            next_action = (
                "Activation unavailable: this form neither verifies nor starts a Job"
                if step == "activate"
                else "Review the verdict: unobserved does not mean proven"
            )
            risk = "medium"
            if step == "activate":
                blocked.append("Server evidence and controlled start are not connected to this form")
    elif errors or blocked:
        status = "error"
        next_action = errors[0] if errors else blocked[0]
        risk = "high"
    else:
        status = "ok"
        next_action = _next_action(step)
        risk = "low"

    return _result(
        step=str(step),
        status=status,
        errors=errors,
        blocked=blocked,
        unproven=unproven,
        declared=declared,
        next_action=next_action,
        risk=risk,
        defaults=_defaults(site),
    )


def _next_action(step: str) -> str:
    environment = _site().fleet_environment
    mapping = {
        "source": "Enter the secret reference; no connection is verified",
        "permissions": "Select tables to copy and the IBM i journal",
        "journal": f"Configure the isolated {environment} Snowflake destination",
        "destination": "Prepare a bounded pilot (10 minutes, replicaCount=0)",
        "pilot": "Read the verdict before activation",
        "verdict": "Activate only a bounded pilot, never replicaCount=1 without a soak",
        "activate": "The permanent runtime remains at replicaCount=0",
    }
    return mapping[step]


def _result(
    *,
    step: str,
    status: str,
    next_action: str,
    risk: str,
    errors: Sequence[str] = (),
    blocked: Sequence[str] = (),
    unproven: Sequence[str] = (),
    declared: Sequence[str] = (),
    defaults: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "step": step,
        "status": status,
        "errors": list(errors),
        "blocked": list(blocked),
        "unproven": list(unproven),
        "proven": [],
        "declared": list(declared),
        "verification_scope": "configuration_only",
        "next_action": next_action,
        "risk": risk,
        "defaults": dict(defaults or _defaults(_site())),
    }


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _secret_keys(payload: Mapping[str, object]) -> tuple[str, ...]:
    found: list[str] = []
    for key in payload:
        normalized = key.lower().replace("-", "_")
        if normalized in _SECRET_KEYS or any(part in normalized for part in ("password", "token", "private_key")):
            found.append(key)
    return tuple(found)
