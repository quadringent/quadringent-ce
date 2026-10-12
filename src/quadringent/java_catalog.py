from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import time
from typing import Callable, Sequence

from .continuous import ReceiverCatalog, ReceiverSnapshot
from .java_worker import PersistentJavaWorker, validate_receiver_identity_request
from .receiver_identity import ReceiverIdentity


CATALOG_HEADER = "as400-receiver-catalog-v2"
LEGACY_CATALOG_HEADER = "as400-receiver-catalog-v1"
TAIL_LINE_PREFIX = "tail "


def parse_catalog_output(output: str) -> tuple[ReceiverSnapshot, ...]:
    """Parse the safe tabular output of the Java JDBC catalog.

    The Java process deliberately emits metadata only. Receiver order is the
    order returned by IBM i after the SQL timestamp ordering and is preserved
    here; no lexical sort is applied.
    """

    lines = [line for line in output.splitlines() if line.strip()]
    if not lines or lines[0].strip() not in (CATALOG_HEADER, LEGACY_CATALOG_HEADER):
        raise ValueError("IBM i receiver catalog header is invalid")
    links_observed = lines[0].strip() == CATALOG_HEADER

    result: list[ReceiverSnapshot] = []
    identities: set[tuple[str, str]] = set()
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != (10 if links_observed else 6) or fields[0] != "receiver":
            raise ValueError("IBM i receiver catalog row is malformed")
        _, receiver_library, receiver, status, first, last = fields[:6]
        links = [None if value == "-" else value for value in fields[6:]]
        identity = (receiver_library, receiver)
        if identity in identities:
            raise ValueError("IBM i receiver catalog contains a duplicate")
        identities.add(identity)
        result.append(
            ReceiverSnapshot(
                receiver_library=receiver_library,
                receiver=receiver,
                status=None if status == "-" else status,
                first_sequence=_optional_sequence(first),
                last_sequence=_optional_sequence(last),
                previous_receiver_library=links[0] if links_observed else None,
                previous_receiver=links[1] if links_observed else None,
                next_receiver_library=links[2] if links_observed else None,
                next_receiver=links[3] if links_observed else None,
                chain_links_observed=links_observed,
            )
        )
    return tuple(result)


def parse_tail_output(output: str) -> ReceiverSnapshot | None:
    """Parse the one-row output of the cheap ATTACHED-receiver probe.

    ``None`` means the probe found no attached receiver (journal detached).
    A required resume must then refresh the catalog before using metadata.
    A single ``tail ...`` row is expected; anything else is a protocol violation.
    """

    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise ValueError("IBM i tail probe returned more than one row")
    line = lines[0]
    if not line.startswith(TAIL_LINE_PREFIX):
        raise ValueError("IBM i tail probe row is malformed")
    fields: dict[str, str] = {}
    for token in line[len(TAIL_LINE_PREFIX):].split():
        key, separator, value = token.partition("=")
        if not separator or not key:
            raise ValueError("IBM i tail probe row is malformed")
        fields[key] = value
    required = ("receiver", "library", "first_sequence", "last_sequence", "status")
    if any(name not in fields for name in required):
        raise ValueError("IBM i tail probe row is malformed")
    return ReceiverSnapshot(
        receiver_library=fields["library"],
        receiver=fields["receiver"],
        status=None if fields["status"] == "-" else fields["status"],
        first_sequence=_optional_sequence(fields["first_sequence"]),
        last_sequence=_optional_sequence(fields["last_sequence"]),
    )


def parse_receiver_identity_output(output: str, *, journal_library: str, journal_name: str,
                                   receiver_library: str, receiver_name: str,
                                   asp_number: int) -> ReceiverIdentity:
    validate_receiver_identity_request(receiver_library=receiver_library,
                                       receiver_name=receiver_name, asp_number=asp_number)
    validate_receiver_identity_request(receiver_library=journal_library,
                                       receiver_name=journal_name, asp_number=asp_number)
    if not isinstance(output, str) or len(output) > 256:
        raise ValueError("IBM i receiver identity output invalid")
    lines = output.splitlines()
    if len(lines) != 2 or lines[0] != "as400-receiver-identity-v1":
        raise ValueError("IBM i receiver identity header or rows invalid")
    fields = lines[1].split("\t")
    if len(fields) != 9 or fields[:5] != ["identity", journal_library, journal_name,
                                        receiver_library, receiver_name]:
        raise ValueError("IBM i receiver identity scope differs")
    values = []
    for token in fields[5:]:
        if re.fullmatch(r"(?:0|[1-9][0-9]*|-[1-9][0-9]*)", token) is None:
            raise ValueError("IBM i receiver identity component invalid")
        value = int(token)
        if not -(2 ** 63) <= value < 2 ** 63:
            raise ValueError("IBM i receiver identity component outside BIGINT")
        values.append(value)
    if values[0] != asp_number:
        raise ValueError("IBM i receiver identity ASP differs")
    return ReceiverIdentity(journal_library=journal_library, journal_name=journal_name,
                            receiver_library=receiver_library, receiver_name=receiver_name,
                            native_generation_kind="ibmi-ifs-object-v1",
                            native_generation=":".join(str(value) for value in values))


class JavaReceiverCatalog(ReceiverCatalog):
    """Invoke the read-only JTOpen/JDBC receiver metadata helper."""

    def __init__(
        self,
        *,
        java: str,
        classpath: str,
        host: str,
        user: str,
        journal_library: str,
        journal_name: str,
        limit: int = 20,
        timeout_seconds: float = 30.0,
        database_port: int | None = None,
        signon_port: int | None = None,
        command_port: int | None = None,
        class_name: str = "io.quadringent.as400.ReadOnlyReceiverCatalog",
        password: str | None = None,
        ca_file: str | None = None,
    ) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("receiver metadata limit must be between 1 and 100")
        if timeout_seconds <= 0:
            raise ValueError("receiver metadata timeout must be positive")
        for name, port in (
            ("database_port", database_port),
            ("signon_port", signon_port),
            ("command_port", command_port),
        ):
            if port is not None and not 1 <= port <= 65535:
                raise ValueError(f"{name} must be between 1 and 65535")
        self.java = java
        self.classpath = classpath
        self.host = host
        self.user = user
        self.journal_library = journal_library
        self.journal_name = journal_name
        self.limit = limit
        self.timeout_seconds = timeout_seconds
        self.database_port = database_port
        self.signon_port = signon_port
        self.command_port = command_port
        self.class_name = class_name
        # Optionnel : les appelants historiques (un seul lecteur par pod)
        # laissent ISERIES_PASSWORD déjà posé dans l'environnement du
        # processus par le Secret monté. Un appelant qui gère plusieurs
        # sources dans le même processus (control plane v2) n'a pas cette
        # garantie et doit fournir le mot de passe explicitement ; il n'est
        # alors posé que dans l'environnement du sous-processus JVM éphémère
        # — jamais journalisé, jamais écrit dans le processus appelant.
        self.password = password
        # Chemin d'un CA épinglé (fichier temporaire 0600 écrit par
        # l'appelant, jamais par cette classe — voir
        # ``v2/executor/boundary_reader.py::JavaBoundaryReader``). ``None``
        # laisse le sous-processus utiliser le magasin de confiance
        # système/JVM par défaut (objectif A du chantier TLS) : c'est
        # pourquoi ``AS400_TLS_CA_FILE`` est explicitement retiré de
        # l'environnement hérité (``os.environ.copy()``) quand ``ca_file``
        # est ``None``, plutôt que de laisser une valeur globale du process
        # appelant fuiter vers une source qui n'a jamais été épinglée.
        self.ca_file = ca_file

    def snapshot(self, required_receiver: str | None = None) -> Sequence[ReceiverSnapshot]:
        environment = os.environ.copy()
        if self.password is not None:
            environment["ISERIES_PASSWORD"] = self.password
        environment.update(
            {
                "ISERIES_HOST": self.host,
                "ISERIES_USER": self.user,
                "AS400_JOURNAL_LIBRARY": self.journal_library,
                "AS400_JOURNAL_NAME": self.journal_name,
                "AS400_RECEIVER_METADATA_LIMIT": str(self.limit),
            }
        )
        if self.ca_file is None:
            environment.pop("AS400_TLS_CA_FILE", None)
        else:
            environment["AS400_TLS_CA_FILE"] = self.ca_file
        if required_receiver is not None and required_receiver.strip():
            environment["AS400_CATALOG_REQUIRES_RECEIVER"] = required_receiver.strip()
        if self.database_port is None:
            environment.pop("AS400_DATABASE_PORT", None)
        else:
            environment["AS400_DATABASE_PORT"] = str(self.database_port)
        if self.signon_port is None:
            environment.pop("AS400_SIGNON_PORT", None)
        else:
            environment["AS400_SIGNON_PORT"] = str(self.signon_port)
        if self.command_port is None:
            environment.pop("AS400_COMMAND_PORT", None)
        else:
            environment["AS400_COMMAND_PORT"] = str(self.command_port)
        try:
            completed = subprocess.run(
                [self.java, "-cp", self.classpath, self.class_name],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("IBM i receiver catalog timed out") from error
        if completed.returncode != 0:
            # Do not propagate stderr: JDBC diagnostics can contain endpoint
            # details and the runtime contract must remain safe by default.
            raise RuntimeError("IBM i receiver catalog failed")
        try:
            return parse_catalog_output(completed.stdout)
        except ValueError as error:
            raise RuntimeError("IBM i receiver catalog output invalid") from error


class WorkerReceiverCatalog:
    """Read receiver metadata through the persistent Java worker JDBC session."""

    def __init__(self, worker: PersistentJavaWorker, *, limit: int = 20) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("receiver metadata limit must be between 1 and 100")
        self.worker = worker
        self.limit = limit

    def snapshot(self, required_receiver: str | None = None) -> Sequence[ReceiverSnapshot]:
        try:
            return parse_catalog_output(
                self.worker.catalog(limit=self.limit, required_receiver=required_receiver)
            )
        except ValueError as error:
            raise RuntimeError("IBM i receiver catalog output invalid") from error

    def identity(self, *, journal_library: str, journal_name: str, receiver_library: str,
                 receiver_name: str, asp_number: int) -> ReceiverIdentity:
        validate_receiver_identity_request(receiver_library=journal_library,
                                           receiver_name=journal_name, asp_number=asp_number)
        validate_receiver_identity_request(receiver_library=receiver_library,
                                           receiver_name=receiver_name, asp_number=asp_number)
        output = self.worker.receiver_identity(receiver_library=receiver_library,
                                               receiver_name=receiver_name, asp_number=asp_number)
        try:
            return parse_receiver_identity_output(output, journal_library=journal_library,
                                                 journal_name=journal_name, receiver_library=receiver_library,
                                                 receiver_name=receiver_name, asp_number=asp_number)
        except ValueError as error:
            raise RuntimeError("IBM i receiver identity output invalid") from error

    def tail(self) -> ReceiverSnapshot | None:
        """Sonde bornee : uniquement le receiver ATTACHED du journal.

        Bien moins cher qu'un catalogue complet (une seule ligne SQL filtree
        sur STATUS='ATTACHED'), utilisee a chaque poll pour detecter une
        nouvelle entree sans refaire une lecture complete du catalogue.
        """

        try:
            return parse_tail_output(self.worker.tail())
        except ValueError as error:
            raise RuntimeError("IBM i tail probe output invalid") from error


class CachedReceiverCatalog:
    """Reuse a receiver snapshot for a few polls during catch-up and tail."""

    def __init__(
        self,
        inner: ReceiverCatalog,
        *,
        ttl_polls: int = 3,
        clock: Callable[[], float] | None = None,
        ttl_seconds: float = 2.0,
        max_stale_reuse: int = 5,
        tail_probe: Callable[[], ReceiverSnapshot | None] | None = None,
    ) -> None:
        if ttl_polls < 1:
            raise ValueError("ttl_polls must be positive")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        if max_stale_reuse < 0:
            raise ValueError("max_stale_reuse must be non-negative")
        self.inner = inner
        self.ttl_polls = ttl_polls
        self.ttl_seconds = ttl_seconds
        self.max_stale_reuse = max_stale_reuse
        self.tail_probe = tail_probe
        self.clock = clock if clock is not None else time.monotonic
        self._cached: tuple[ReceiverSnapshot, ...] | None = None
        self._remaining = 0
        self._expires_at = 0.0
        self.fetch_count = 0
        self.stale_count = 0
        self.consecutive_stale = 0

    def snapshot(self, required_receiver: str | None = None) -> Sequence[ReceiverSnapshot]:
        now = self.clock()
        # Un hit n'est valable que si la vue contient le receiver requis : un
        # catalogue récent qui ne couvre pas le checkpoint forcerait un refus
        # de planification alors que la donnée est en ligne.
        if (
            self._cached is not None
            and self._remaining > 0
            and now < self._expires_at
            and (required_receiver is None or _covers(self._cached, required_receiver))
        ):
            if required_receiver is not None or self.tail_probe is not None:
                self._apply_tail_probe(required_receiver)
                if required_receiver is not None and self._cached is not None:
                    now = self.clock()
            # La sonde peut avoir invalidé le cache (rotation détectée) : le
            # hit n'est plus valable, on retombe sur la lecture complète.
            if (
                self._cached is not None
                and self._remaining > 0
                and now < self._expires_at
                and (required_receiver is None or _covers(self._cached, required_receiver))
            ):
                self._remaining -= 1
                return self._cached
        try:
            snapshot = tuple(self.inner.snapshot(required_receiver))
        except Exception:
            # Diagnostics may reuse bounded stale metadata. A required resume
            # needs fresh status and bounds before authorizing a read.
            if (
                required_receiver is not None
                or self._cached is None
                or self.consecutive_stale >= self.max_stale_reuse
            ):
                raise
            self.stale_count += 1
            self.consecutive_stale += 1
            self._remaining = 0
            self._expires_at = now
            return self._cached
        self.fetch_count += 1
        self.consecutive_stale = 0
        self._cached = snapshot
        self._remaining = self.ttl_polls - 1
        self._expires_at = now + self.ttl_seconds
        return snapshot

    def invalidate(self) -> None:
        self._cached = None
        self._remaining = 0
        self._expires_at = 0.0

    def on_idle(self) -> None:
        """Garde le catalogue si la sonde du receveur suit sa séquence.

        Sans sonde, la prochaine lecture complète reste nécessaire pour
        découvrir une nouvelle écriture. Avec elle, invalider à chaque poll
        oisif annulerait le cache et relirait tout le catalogue IBM i.
        """
        if self.tail_probe is None:
            self.invalidate()

    def _apply_tail_probe(self, required_receiver: str | None = None) -> None:
        """Refresh bounds only when the tail attests the required receiver."""

        assert self._cached is not None
        attached = _attached(self._cached)
        if required_receiver is not None and (
            self.tail_probe is None
            or attached is None
            or attached.receiver != required_receiver
            or attached.first_sequence is None
            or attached.last_sequence is None
        ):
            self.invalidate()
            return
        assert self.tail_probe is not None
        try:
            probed = self.tail_probe()
        except Exception:
            if required_receiver is not None:
                self.invalidate()
            return
        if probed is None:
            if required_receiver is not None:
                self.invalidate()
            return
        if (
            attached is None
            or attached.receiver_library != probed.receiver_library
            or attached.receiver != probed.receiver
        ):
            self.invalidate()
            return
        if required_receiver is not None and (
            probed.status != "ATTACHED"
            or probed.first_sequence != attached.first_sequence
            or probed.last_sequence is None
            or probed.last_sequence < attached.last_sequence
        ):
            self.invalidate()
            return
        new_last = attached.last_sequence
        if probed.last_sequence is not None and (
            new_last is None or probed.last_sequence > new_last
        ):
            new_last = probed.last_sequence
        if new_last == attached.last_sequence and (
            probed.status is None or probed.status == attached.status
        ):
            return
        updated = dataclasses.replace(
            attached,
            last_sequence=new_last,
            status=probed.status if probed.status is not None else attached.status,
        )
        self._cached = tuple(
            updated if item is attached else item for item in self._cached
        )


def _attached(snapshots: Sequence[ReceiverSnapshot]) -> ReceiverSnapshot | None:
    for item in snapshots:
        if item.status == "ATTACHED":
            return item
    return None


def _covers(snapshots: Sequence[ReceiverSnapshot], receiver: str) -> bool:
    return any(item.receiver == receiver for item in snapshots)


def _optional_sequence(value: str) -> int | None:
    if value == "-":
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError("IBM i receiver sequence is invalid") from error
