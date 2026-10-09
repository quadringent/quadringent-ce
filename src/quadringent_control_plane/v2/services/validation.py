"""Validation réutilisée depuis les regex de ``connections.py`` (v1).

Ce module ne redéfinit aucune règle : il importe directement les motifs
compilés par ``connections.py`` pour que toute évolution de ces regex se
propage sans divergence entre v1 et v2 — le contrat (§9.2, tâche 2) exige
une « régression sur les regex reprises de connections.py ».
"""

from __future__ import annotations

from ...connections import (
    _DISPLAY_NAME,
    _IBMI_HOST,
    _IBMI_USER,
    _SNOWFLAKE_ACCOUNT,
)


class ValidationError(ValueError):
    """Un champ ne respecte pas son motif — jamais un secret dans le message."""


def validated_display_name(value: object) -> str:
    if not isinstance(value, str) or _DISPLAY_NAME.fullmatch(value) is None:
        raise ValidationError("Invalid display_name")
    return value


def validated_ibmi_host(value: object) -> str:
    if not isinstance(value, str) or _IBMI_HOST.fullmatch(value) is None:
        raise ValidationError("Invalid ibmi_host")
    return value


def validated_ibmi_user(value: object) -> str:
    if not isinstance(value, str) or _IBMI_USER.fullmatch(value) is None:
        raise ValidationError("Invalid ibmi_user")
    return value


def validated_snowflake_account(value: object) -> str:
    if not isinstance(value, str) or _SNOWFLAKE_ACCOUNT.fullmatch(value) is None:
        raise ValidationError("Invalid snowflake_account")
    return value
