"""Bounded native receiver observations; sampled identities establish no stable cut."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Callable, Mapping

from ..continuous import ReceiverSnapshot, require_readable_receiver
from ..java_catalog import CATALOG_HEADER, parse_catalog_output, parse_receiver_identity_output
from ..java_worker import validate_receiver_identity_request
from ..receiver_identity import ReceiverIdentity
from .verification_contract import VerificationScope
from .verification_guard import _Budget, _read_original
from .verification_process import DocumentLimits, collect_process_document

_FORMAT = "quadringent-native-receiver-observation-v1"
_CLASS = "io.quadringent.as400.NativeVerificationReceiverExport"
_FIELDS = {
    "format_version",
    "scope_sha256",
    "phase",
    "identity_before_base64",
    "catalog_base64",
    "identity_after_base64",
}
_ENVIRONMENT = {
    "PATH",
    "JAVA_HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "AS400_TLS_CA_FILE",
    "AS400_CATALOG_QUERY_TIMEOUT_SECONDS",
}
_MAX_BYTES = 1024 * 1024


class ReceiverObservationError(ValueError):
    """A failed observation without producer output or credentials in its message."""


@dataclass(frozen=True)
class ReceiverObservationConfig:
    java: str
    classpath: str
    producer_revision: str
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    max_bytes: int = 512 * 1024
    max_seconds: int = 60

    def __post_init__(self):
        for value in (self.java, self.classpath):
            if type(value) is not str or not value or len(value) > 8192 or any(ord(char) < 32 for char in value):
                raise ReceiverObservationError("invalid_receiver_configuration")
        if (
            type(self.producer_revision) is not str
            or re.fullmatch(r"[0-9a-f]{40}", self.producer_revision) is None
            or type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= _MAX_BYTES
            or type(self.max_seconds) is not int
            or not 1 <= self.max_seconds <= 60
            or not isinstance(self.environment, Mapping)
            or not set(self.environment) <= _ENVIRONMENT
        ):
            raise ReceiverObservationError("invalid_receiver_configuration")
        for key, value in self.environment.items():
            if type(key) is not str or type(value) is not str or len(value) > 8192 or "\0" in value:
                raise ReceiverObservationError("invalid_receiver_configuration")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def command(self, scope: VerificationScope, phase: str) -> tuple[str, ...]:
        identity = _scope_identity(scope, self)
        _phase(phase)
        return (
            self.java,
            "-cp",
            self.classpath,
            _CLASS,
            identity.journal_library,
            identity.journal_name,
            identity.receiver_library,
            identity.receiver_name,
            "1",
            scope.sha256,
            phase,
        )


@dataclass(frozen=True)
class ReceiverOriginal:
    path: Path | None
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class ReceiverObservation:
    original: ReceiverOriginal
    content: bytes = field(repr=False)
    scope: VerificationScope = field(repr=False)
    phase: str
    receiver: ReceiverSnapshot
    catalog: tuple[ReceiverSnapshot, ...] = field(repr=False)
    _native_originals: tuple[tuple[str, bytes], ...] = field(repr=False)

    @property
    def native_originals(self) -> dict[str, bytes]:
        return dict(self._native_originals)

    @property
    def metadata(self) -> dict:
        return {
            "format_version": _FORMAT,
            "scope_sha256": self.scope.sha256,
            "phase": self.phase,
            "producer_revision": self.scope.producer_revision,
            "receiver_identity": self.scope.copy_boundary.receiver_identity.to_dict(),
            "catalog_receiver_count": len(self.catalog),
            "first_sequence": self.receiver.first_sequence,
            "last_sequence": self.receiver.last_sequence,
            "status": self.receiver.status,
            "bootstrap": self.scope.capture_binding.bootstrap.to_dict(),
            "copy_boundary": self.scope.copy_boundary.to_dict(),
            "checkpoint": self.scope.checkpoint.to_dict(),
            "receiver_windows": [window.to_dict() for window in self.scope.receiver_windows],
            "coverage": "same_receiver_observed_ranges_only",
            "full_chain_established": False,
            "native_admission": "not_evaluated",
            "stable_cut_established": False,
        }


@dataclass(frozen=True)
class ReceiverCoverage:
    before: ReceiverObservation
    after: ReceiverObservation

    @property
    def metadata(self) -> dict:
        return {
            **self.after.metadata,
            "phase": "before_after",
            "before_sha256": self.before.original.sha256,
            "after_sha256": self.after.original.sha256,
            "identity_matches_at_observations": True,
            "lifecycle_between_observations_established": False,
        }


def _phase(value):
    if type(value) is not str or value not in {"before", "after"}:
        raise ReceiverObservationError("invalid_receiver_phase")


def _scope_identity(scope, config):
    if type(scope) is not VerificationScope or type(config) is not ReceiverObservationConfig:
        raise ReceiverObservationError("invalid_receiver_configuration")
    if scope.producer_revision != config.producer_revision:
        raise ReceiverObservationError("receiver_producer_revision_differs")
    identity = scope.copy_boundary.receiver_identity
    if (
        len(scope.receiver_windows) != 1
        or any(
            point.receiver_identity != identity
            for point in (scope.capture_binding.bootstrap, scope.copy_boundary, scope.checkpoint)
        )
        or scope.receiver_windows[0].identity != identity
        or identity.native_generation_kind != "ibmi-ifs-object-v1"
    ):
        raise ReceiverObservationError("unsupported_receiver_scope")
    pieces = identity.native_generation.split(":")
    if (
        len(pieces) != 4
        or pieces[0] != "1"
        or any(re.fullmatch(r"(?:0|[1-9][0-9]*|-[1-9][0-9]*)", value) is None for value in pieces)
        or any(not -(2**63) <= int(value) < 2**63 for value in pieces)
    ):
        raise ReceiverObservationError("unsupported_receiver_generation")
    return identity


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ReceiverObservationError("duplicate_receiver_field")
        value[key] = item
    return value


def _constant(_):
    raise ReceiverObservationError("nonfinite_receiver_number")


def _native_text(value, maximum):
    if type(value) is not str or len(value) > maximum * 2:
        raise ReceiverObservationError("invalid_receiver_original")
    raw = b64decode(value, validate=True)
    if not 1 <= len(raw) <= maximum or b64encode(raw).decode("ascii") != value:
        raise ReceiverObservationError("invalid_receiver_original")
    text = raw.decode("utf-8")
    if not text.endswith("\n") or "\r" in text or "\0" in text:
        raise ReceiverObservationError("invalid_receiver_original")
    return raw, text


def _catalog(text):
    lines = text.splitlines()
    if not lines or lines[0] != CATALOG_HEADER or any(not line for line in lines) or len(lines) >= 1001:
        # The anchored native query stops at 1000; a full page cannot certify EOF.
        raise ReceiverObservationError("incomplete_receiver_catalog")
    for line in lines[1:]:
        tokens = line.split("\t")
        if len(tokens) != 10 or tokens[0] != "receiver":
            raise ReceiverObservationError("invalid_receiver_catalog")
        validate_receiver_identity_request(receiver_library=tokens[1], receiver_name=tokens[2], asp_number=1)
        for library, name in ((tokens[6], tokens[7]), (tokens[8], tokens[9])):
            if (library, name) != ("-", "-"):
                validate_receiver_identity_request(receiver_library=library, receiver_name=name, asp_number=1)
        if (
            tokens[3] != "-"
            and re.fullmatch(r"[A-Z][A-Z_]{0,31}", tokens[3]) is None
            or any(token != "-" and re.fullmatch(r"(?:0|[1-9][0-9]{0,20})", token) is None for token in tokens[4:6])
        ):
            raise ReceiverObservationError("invalid_receiver_catalog")
    return parse_catalog_output(text)


def _read(content, scope, config):
    identity = _scope_identity(scope, config)
    if type(content) is not bytes or not 1 <= len(content) <= config.max_bytes:
        raise ReceiverObservationError("receiver_byte_budget")
    document = json.loads(content.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    if (
        type(document) is not dict
        or set(document) != _FIELDS
        or document["format_version"] != _FORMAT
        or document["scope_sha256"] != scope.sha256
    ):
        raise ReceiverObservationError("receiver_observation_scope_or_schema")
    _phase(document["phase"])
    originals = []
    for name in ("identity_before", "catalog", "identity_after"):
        raw, text = _native_text(document[name + "_base64"], 256 if name != "catalog" else 256 * 1024)
        originals.append((name, raw))
        if name != "catalog":
            observed = parse_receiver_identity_output(
                text,
                journal_library=identity.journal_library,
                journal_name=identity.journal_name,
                receiver_library=identity.receiver_library,
                receiver_name=identity.receiver_name,
                asp_number=1,
            )
            if observed != identity:
                raise ReceiverObservationError("receiver_generation_changed")
        else:
            catalog = _catalog(text)
    selected = [
        row
        for row in catalog
        if (row.receiver_library, row.receiver) == (identity.receiver_library, identity.receiver_name)
    ]
    if len(selected) != 1:
        raise ReceiverObservationError("required_receiver_missing")
    receiver = selected[0]
    require_readable_receiver(receiver)
    if receiver.first_sequence is None or receiver.last_sequence is None:
        raise ReceiverObservationError("receiver_ranges_missing")
    for point in (scope.capture_binding.bootstrap, scope.copy_boundary, scope.checkpoint):
        if not receiver.first_sequence - 1 <= point.sequence <= receiver.last_sequence:
            raise ReceiverObservationError("receiver_ranges_do_not_cover_scope")
    window = scope.receiver_windows[0]
    if window.start_sequence <= window.end_sequence and not (
        receiver.first_sequence <= window.start_sequence <= window.end_sequence <= receiver.last_sequence
    ):
        raise ReceiverObservationError("receiver_window_not_covered")
    original = ReceiverOriginal(None, hashlib.sha256(content).hexdigest(), len(content))
    return ReceiverObservation(original, content, scope, document["phase"], receiver, catalog, tuple(originals))


def read_receiver_observation(
    content: bytes, *, scope: VerificationScope, expected_config: ReceiverObservationConfig
) -> ReceiverObservation:
    """Reparse archived native originals; serialized claims cannot issue an admission."""
    try:
        return _read(content, scope, expected_config)
    except Exception:
        raise ReceiverObservationError("invalid_receiver_observation") from None


def observe_receiver(
    scope: VerificationScope,
    *,
    phase: str,
    directory: Path,
    config: ReceiverObservationConfig,
    input_bytes: bytes,
    check_current: Callable[[], bool] = lambda: True,
) -> ReceiverObservation:
    try:
        return _observe_receiver(scope, phase, directory, config, input_bytes, check_current)
    except ReceiverObservationError:
        raise
    except Exception:
        raise ReceiverObservationError("receiver_observation_failed") from None


def _observe_receiver(scope, phase, directory, config, input_bytes, check_current):
    command = config.command(scope, phase)
    budget = _Budget(config.max_seconds, check_current)

    def validate(document):
        budget.check()
        parsed = read_receiver_observation(
            json.dumps(document, separators=(",", ":")).encode(), scope=scope, expected_config=config
        )
        if parsed.phase != phase:
            raise ReceiverObservationError("receiver_phase_differs")

    budget.check()
    result = collect_process_document(
        command,
        directory=directory,
        input_bytes=input_bytes,
        environment=config.environment,
        validate=validate,
        limits=DocumentLimits(max_bytes=config.max_bytes, max_seconds=max(1, int(budget.check()))),
    )
    if type(result.return_code) is not int or result.return_code != 0:
        raise ReceiverObservationError("receiver_process_exit")
    content, original, _ = _read_original(result.path, config.max_bytes, budget)
    if original.sha256 != result.sha256 or original.byte_count != result.byte_count:
        raise ReceiverObservationError("receiver_original_changed")
    parsed = read_receiver_observation(content, scope=scope, expected_config=config)
    budget.check()
    return replace(parsed, original=ReceiverOriginal(original.path, original.sha256, original.byte_count))


def validate_receiver_pair(
    scope: VerificationScope,
    before: ReceiverObservation,
    after: ReceiverObservation,
    *,
    expected_config: ReceiverObservationConfig,
) -> ReceiverCoverage:
    """Compare sampled generations and ranges; lifecycle between reads remains unproven."""
    parsed = []
    for phase, value in (("before", before), ("after", after)):
        if type(value) is not ReceiverObservation:
            raise ReceiverObservationError("invalid_receiver_pair")
        current = read_receiver_observation(value.content, scope=scope, expected_config=expected_config)
        if (
            current.phase != phase
            or current.original.sha256 != value.original.sha256
            or current.original.byte_count != value.original.byte_count
        ):
            raise ReceiverObservationError("invalid_receiver_pair")
        parsed.append(replace(current, original=value.original))
    first, last = parsed
    if (
        first.receiver.first_sequence != last.receiver.first_sequence
        or last.receiver.last_sequence < first.receiver.last_sequence
    ):
        raise ReceiverObservationError("receiver_range_truncated_or_changed")
    return ReceiverCoverage(first, last)
