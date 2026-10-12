"""Découverte de tables IBM i (tâche 4) : parsing du protocole worker,
classification de disponibilité (readiness) et génération des commandes CL
correctives.

Le worker Java (``PersistentJournalWorker``, commande ``discover``) émet un
catalogue seulement : aucune ligne, aucune donnée métier. Une ligne
``table\\t...`` par table trouvée (voir ``TableDiscovery.formatRow`` côté
Java), puis ``discover_done`` ou ``discover_error=<Classe>`` — ce module ne
lit que le flux déjà capté par ``PersistentJavaWorker.discover`` (voir
``java_worker.py``), jamais de connexion réseau.

Sources pour les commandes CL générées (syntaxe vérifiée sur IBM i 7.4/7.5,
référence CL command reference du IBM i Knowledge Center — Programming ->
Control language) :
  - CRTJRNRCV (Create Journal Receiver), THRESHOLD default 1500000 KB :
    https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/crtjrnrcv.htm
  - CRTJRN (Create Journal), MNGRCV(*SYSTEM) needs a receiver threshold
    other than *NONE :
    https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/crtjrn.htm
  - STRJRNPF (Start Journal Physical File) :
    https://www.ibm.com/docs/en/i/7.5?topic=sjpf-start-journal-physical-file-strjrnpf
  - CHGJRNOBJ (Change Journaled Object) :
    https://www.ibm.com/docs/en/i/7.5?topic=cjo-change-journaled-object-chgjrnobj
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

DISCOVER_ROW_TAG = "table"
_ROW_FIELD_COUNT = 15

# Readiness — un état par table (§2 du plan produit, écran « Tables »).
READY = "ready"
NOT_JOURNALED = "not_journaled"
IMAGES_INCOMPLETE = "images_incomplete"
NO_KEY = "no_key"
JOURNAL_MISMATCH = "journal_mismatch"

_RRN_CONSEQUENCE_SENTENCE = (
    "identified by its physical position (RRN) — a file reorganization "
    "(RGZPFM, CLRPFM) will require resynchronization"
)


class DiscoverProtocolError(ValueError):
    """La sortie du worker ``discover`` ne respecte pas le protocole ligne attendu."""


@dataclass(frozen=True)
class DiscoveredColumn:
    """Une colonne rapportée par ``QSYS2.SYSCOLUMNS`` (``TableDiscovery.java``).

    ``type`` est le type SQL natif tel que rendu par le catalogue (ex.
    ``DECIMAL``, ``VARCHAR``) — pas encore traduit en ``kind`` Snowflake
    (voir ``ibmi_catalog_type_to_kind``). L'ordre (ordinal) est porté par la
    position dans ``DiscoveredTable.columns``, jamais un champ séparé.
    """

    name: str
    type: str
    length: int | None
    scale: int | None
    nullable: bool


@dataclass(frozen=True)
class DiscoveredTable:
    """Une table trouvée par ``discover`` — catalogue seulement, jamais de ligne métier."""

    library: str
    system_name: str
    sql_name: str
    text: str | None
    row_count: int | None
    size_bytes: int | None
    has_key: bool
    key_columns: tuple[str, ...]
    journaled: bool
    journal_library: str | None
    journal_name: str | None
    images: str | None
    omitted: bool
    columns: tuple[DiscoveredColumn, ...] = ()

    @property
    def qualified_name(self) -> str:
        return f"{self.library}/{self.system_name}"

    @property
    def journal_qualified_name(self) -> str | None:
        if self.journal_library is None or self.journal_name is None:
            return None
        return f"{self.journal_library}/{self.journal_name}"


def parse_discover_output(output: str) -> tuple[DiscoveredTable, ...]:
    """Parse la sortie captée de ``PersistentJavaWorker.discover`` (avant ``discover_done``).

    Chaque ligne non vide doit commencer par ``table\\t`` et porter exactement
    15 champs tabulés (voir ``TableDiscovery.formatRow`` côté Java). Une
    ligne malformée est fail-closed : ``DiscoverProtocolError``, jamais une
    table ignorée silencieusement.
    """

    tables: list[DiscoveredTable] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != _ROW_FIELD_COUNT or fields[0] != DISCOVER_ROW_TAG:
            raise DiscoverProtocolError(f"malformed discovery row: {raw_line!r}")
        (
            _,
            library,
            system_name,
            sql_name,
            text,
            row_count,
            size_bytes,
            has_key,
            key_columns_csv,
            journaled,
            journal_library,
            journal_name,
            images,
            omitted,
            columns_field,
        ) = fields
        # Les noms IBM i (bibliothèque, fichier système, journal) sont des CHAR(10)
        # complétés d'espaces dans le catalogue : on les compare et on les affiche nus.
        library, system_name, sql_name = library.strip(), system_name.strip(), sql_name.strip()
        journal_library, journal_name = journal_library.strip(), journal_name.strip()
        identity = (library, system_name)
        if identity in seen:
            raise DiscoverProtocolError(f"duplicate table in discovery: {identity}")
        seen.add(identity)
        key_columns = tuple(c.strip() for c in key_columns_csv.split(",") if c.strip()) if key_columns_csv else ()
        tables.append(
            DiscoveredTable(
                library=library,
                system_name=system_name,
                sql_name=sql_name,
                text=text or None,
                row_count=int(row_count) if row_count else None,
                size_bytes=int(size_bytes) if size_bytes else None,
                has_key=_parse_yes_no(has_key),
                key_columns=key_columns,
                journaled=_parse_yes_no(journaled),
                journal_library=journal_library or None,
                journal_name=journal_name or None,
                images=images or None,
                omitted=_parse_yes_no(omitted),
                columns=_parse_columns_field(columns_field),
            )
        )
    return tuple(tables)


def _parse_columns_field(field: str) -> tuple[DiscoveredColumn, ...]:
    """Reconstruit les colonnes sérialisées par ``TableDiscovery.formatColumns``
    (Java) : ``nom,type,longueur,échelle,nullable`` par colonne, colonnes
    séparées par ``;``. Même contrat des deux côtés — testé en miroir côté
    Java (``TableDiscoveryTest``) et ici."""

    if not field:
        return ()
    columns: list[DiscoveredColumn] = []
    for entry in field.split(";"):
        parts = entry.split(",")
        if len(parts) != 5:
            raise DiscoverProtocolError(f"malformed discovery column: {entry!r}")
        name, sql_type, length, scale, nullable = parts
        columns.append(
            DiscoveredColumn(
                name=name,
                type=sql_type,
                length=int(length) if length else None,
                scale=int(scale) if scale else None,
                nullable=_parse_yes_no(nullable),
            )
        )
    return tuple(columns)


def _parse_yes_no(value: str) -> bool:
    if value not in ("yes", "no"):
        raise DiscoverProtocolError(f"invalid yes/no field in discovery: {value!r}")
    return value == "yes"


@dataclass(frozen=True)
class ClCommand:
    """Une commande CL corrective, avec sa justification en français."""

    command: str
    reason: str


@dataclass(frozen=True)
class TableReadiness:
    """Résultat de classification pour une table : état + commandes CL le cas échéant."""

    table: DiscoveredTable
    state: str
    explanation: str
    cl_commands: tuple[ClCommand, ...] = field(default_factory=tuple)


# IBM i object names: at most 10 characters, starting with a letter, $, # or @.
_IBMI_NAME = re.compile(r"[A-Z$#@][A-Z0-9$#@_.]{0,9}")


def _valid_ibmi_name(value: str | None) -> bool:
    return value is not None and _IBMI_NAME.fullmatch(value) is not None


def classify_table(
    table: DiscoveredTable,
    *,
    has_journal_in_library: bool = True,
    existing_journal: tuple[str, str] | None = None,
) -> TableReadiness:
    """Classe une table seule, sans tenir compte des autres tables sélectionnées.

    L'état ``journal_mismatch`` est intrinsèquement une propriété d'une
    *sélection* (plusieurs tables journalisées dans des journaux différents),
    voir :func:`classify_selection`.
    """

    if not table.journaled:
        explanation = (
            f"{table.qualified_name} is not journaled: change capture is "
            "impossible without an active journal on this physical file."
        )
        if not (_valid_ibmi_name(table.library) and _valid_ibmi_name(table.system_name)):
            return TableReadiness(
                table=table,
                state=NOT_JOURNALED,
                explanation=explanation + " Its name cannot be used in a generated CL command; "
                "start journaling manually.",
                cl_commands=(),
            )
        commands = _journal_setup_commands(
            table, has_journal_in_library=has_journal_in_library, existing_journal=existing_journal
        )
        return TableReadiness(table=table, state=NOT_JOURNALED, explanation=explanation, cl_commands=commands)
    if table.images != "*BOTH":
        images_label = table.images or "none"
        return TableReadiness(
            table=table,
            state=IMAGES_INCOMPLETE,
            explanation=(
                f"{table.qualified_name} is journaled with {images_label} : "
                "without the before image (*BOTH), deletes and updates cannot "
                "be replicated correctly (missing before values)."
            ),
            cl_commands=(
                ClCommand(
                    command=(
                        f"CHGJRNOBJ OBJ(({table.library}/{table.system_name} *FILE)) "
                        "ATR(*IMAGES) IMAGES(*BOTH)"
                    ),
                    reason="Switch journal images from *AFTER to *BOTH without resetting the journal.",
                ),
            ),
        )
    if not table.has_key:
        return TableReadiness(
            table=table,
            state=NO_KEY,
            explanation=(
                f"{table.qualified_name} has neither a primary key nor a unique index: "
                f"it will be {_RRN_CONSEQUENCE_SENTENCE}."
            ),
            cl_commands=(),
        )
    return TableReadiness(
        table=table,
        state=READY,
        explanation=(
            f"{table.qualified_name} is ready: journaled with *BOTH and a key "
            f"({', '.join(table.key_columns)})."
        ),
        cl_commands=(),
    )


def _journal_setup_commands(
    table: DiscoveredTable,
    *,
    has_journal_in_library: bool,
    existing_journal: tuple[str, str] | None = None,
) -> tuple[ClCommand, ...]:
    commands: list[ClCommand] = []
    if existing_journal is not None and all(_valid_ibmi_name(part) for part in existing_journal):
        journal_library, journal_name = existing_journal
        journal_reason = f"Journal {journal_library}/{journal_name} already journals other tables of this library."
    elif has_journal_in_library:
        journal_library, journal_name = table.library, _DEFAULT_JOURNAL_NAME
        journal_reason = (
            f"Assumes the library journal is {journal_name} (the SQL schema default); "
            "replace it with the journal that already exists in this library."
        )
    else:
        journal_library, journal_name = table.library, _DEFAULT_JOURNAL_NAME
        receiver_name = _DEFAULT_RECEIVER_NAME
        journal_reason = "Uses the journal created by the previous commands."
        commands.append(
            ClCommand(
                command=(
                    f"CRTJRNRCV JRNRCV({journal_library}/{receiver_name}) "
                    "TEXT('Quadringent journal receiver')"
                ),
                reason=(
                    "No usable journal in the library: create its receiver first. THRESHOLD is "
                    "omitted, so the IBM default of 1500000 KB applies; review it against your "
                    "receiver retention plan before running."
                ),
            )
        )
        commands.append(
            ClCommand(
                command=(
                    f"CRTJRN JRN({journal_library}/{journal_name}) "
                    f"JRNRCV({journal_library}/{receiver_name}) MNGRCV(*SYSTEM) DLTRCV(*NO) "
                    "TEXT('Quadringent journal')"
                ),
                reason=(
                    "Create the journal with system-managed receiver changes at the threshold. "
                    "DLTRCV(*NO) keeps detached receivers until your own retention procedure "
                    "removes them after Quadringent has read them; review before running."
                ),
            )
        )
    commands.append(
        ClCommand(
            command=(
                f"STRJRNPF FILE({table.library}/{table.system_name}) "
                f"JRN({journal_library}/{journal_name}) IMAGES(*BOTH) OMTJRNE(*OPNCLO)"
            ),
            reason=(
                "Start journaling this physical file with both images "
                "(before/after); OMTJRNE(*OPNCLO) omits open/close entries, "
                "which are not required for replication. " + journal_reason
            ),
        )
    )
    return tuple(commands)


_DEFAULT_JOURNAL_NAME = "QSQJRN"
_DEFAULT_RECEIVER_NAME = "QSQJRN0001"


@dataclass(frozen=True)
class SelectionReadiness:
    """Classification d'un ensemble de tables sélectionnées pour un même pipeline."""

    per_table: tuple[TableReadiness, ...]
    journal_mismatch: bool
    journal_mismatch_explanation: str | None


def classify_selection(
    tables: tuple[DiscoveredTable, ...], *, has_journal_in_library: bool = True
) -> SelectionReadiness:
    """Classe une sélection de tables, en ajoutant la détection de journaux distincts.

    Le mésappariement de journal (``journal_mismatch``) ne bloque aucune
    table individuellement — il est signalé au niveau de la sélection car il
    implique un lecteur de journal distinct par journal, ce que l'écran
    « Tables » doit expliquer avant l'activation du pipeline.
    """

    per_table = tuple(
        classify_table(table, has_journal_in_library=has_journal_in_library) for table in tables
    )
    journals = {
        readiness.table.journal_qualified_name
        for readiness in per_table
        if readiness.table.journaled and readiness.table.journal_qualified_name is not None
    }
    mismatch = len(journals) > 1
    explanation = None
    if mismatch:
        ordered = sorted(journals)
        explanation = (
            f"The selected tables use {len(ordered)} journaux distincts "
            f"({', '.join(ordered)} ): an independent journal reader will be "
            "required per journal, each with its own throughput and "
            "errors — this does not block activation, but the pipeline will account for it."
        )
    return SelectionReadiness(
        per_table=per_table, journal_mismatch=mismatch, journal_mismatch_explanation=explanation
    )


# --- Colonnes découvertes -> type Snowflake (objectif C, chantier 2026-09-24) ---
#
# ``QSYS2.SYSCOLUMNS.DATA_TYPE`` rend le nom SQL court du type (forme
# documentée par IBM pour ce catalogue) — pas encore vérifié sur un IBM i
# réel par ce chantier (voir « limites » du rapport de commit). Le
# vocabulaire cible est celui de ``quadringent.snowflake_destination.
# IbmiColumnType.kind`` ; un type absent de cette table est laissé de côté
# (jamais deviné) — la table reste alors sans ``discovered_columns``
# automatiques, déclarable via ``PUT .../discovered-columns``.
_CATALOG_TYPE_TO_KIND = {
    "CHAR": "char",
    "CHARACTER": "char",
    "VARCHAR": "varchar",
    "CHARACTER VARYING": "varchar",
    "GRAPHIC": "graphic",
    "VARGRAPHIC": "vargraphic",
    "SMALLINT": "smallint",
    "INTEGER": "integer",
    "INT": "integer",
    "BIGINT": "bigint",
    "DECIMAL": "decimal",
    "NUMERIC": "numeric",
    "DATE": "date",
    "TIME": "time",
    "TIMESTAMP": "timestamp",
    # Abréviations sur 8 caractères de SYSCOLUMNS (Db2 for i) : TIMESTMP
    # constaté sur IBM i 7.5 ; VARG et VARBIN suivent la même convention.
    "TIMESTMP": "timestamp",
    "VARG": "vargraphic",
    "VARBIN": "varbinary",
    "BINARY": "binary",
    "VARBINARY": "varbinary",
    "CLOB": "clob",
    "DBCLOB": "dbclob",
    "BLOB": "blob",
}

# Types dont ``LENGTH`` (SYSCOLUMNS) désigne la précision totale, pas une
# longueur de stockage — reflète ``IbmiColumnType`` (``length`` vs
# ``precision``, voir ``snowflake_destination.py``).
_DECIMAL_LIKE_KINDS = frozenset({"decimal", "numeric"})


def ibmi_catalog_type_to_kind(raw_type: str) -> str | None:
    """Traduit ``DATA_TYPE`` (SYSCOLUMNS) en ``IbmiColumnType.kind`` — ``None``
    si le type n'a pas de correspondance connue (jamais une conversion
    devinée)."""

    return _CATALOG_TYPE_TO_KIND.get(raw_type.strip().upper())


def discovered_column_to_type_kwargs(column: DiscoveredColumn) -> dict[str, object] | None:
    """Construit les arguments de ``IbmiColumnType`` pour une colonne
    découverte, ou ``None`` si son type catalogue n'est pas reconnu.

    ``LENGTH`` va vers ``precision`` pour DECIMAL/NUMERIC (le catalogue y
    rapporte le nombre total de chiffres, pas une longueur de stockage),
    vers ``length`` pour les autres types bornés."""

    kind = ibmi_catalog_type_to_kind(column.type)
    if kind is None:
        return None
    if kind in _DECIMAL_LIKE_KINDS:
        return {"kind": kind, "precision": column.length, "scale": column.scale}
    return {"kind": kind, "length": column.length}
