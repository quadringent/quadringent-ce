"""Helpers of the ``quadringent local`` host CLI tests: no network, no Docker, no real secret."""
from __future__ import annotations

import io
import json
from pathlib import Path

from quadringent.installer.cli import _build_parser
from quadringent.installer.runner import CommandResult, RecordingRunner
from quadringent.local.cli import run_local_command
from quadringent.local import config as cfg

PASSWORD = "Pw-offline-4f9a1c7e"
IBMI_HOST = "ibmi.example.test"
RESULT = "quadringent_local_result="
TZ = "Europe/Paris"
RUN_ID = "0d6a9c2e-1a2b-4c3d-9e0f-1234567890ab"

# One catalog row per the worker protocol: 15 tab-separated fields (see table_discovery.parse_discover_output).
COLUMNS = "ID,DECIMAL,9,0,no;NAME,VARCHAR,40,,yes;CREATED,TIMESTAMP,,,yes"


def table_row(*, library="QDCLIB", name="ORDERS", has_key="yes", keys="ID", journaled="yes", images="*BOTH",
              columns=COLUMNS, journal=("QDCLIB", "QSQJRN")) -> str:
    fields = ["table", library, name, name, "Orders", "10", "4096", has_key, keys, journaled,
              journal[0] if journaled == "yes" else "", journal[1] if journaled == "yes" else "",
              images if journaled == "yes" else "", "no", columns]
    return "\t".join(fields)


def discover_output(*rows: str) -> str:
    return "quadringent_discover_result=" + json.dumps({"raw_output": "\n".join(rows) + "\ndiscover_done\n"}) + "\n"


def probe_output(*, network=True, tls=True, trust="system", auth=True, auth_detail="authentication succeeded",
                 qtimzon="QP0100CET", zone="IBM:QP0100CET") -> str:
    document = {
        "reachable": network and tls and auth,
        "network": {"ok": network, "detail": "TCP connection established on 9476" if network else "unreachable"},
        "tls": {"ok": tls, "detail": "TLS handshake succeeded", "fingerprint": "ab" * 32, "trust": trust,
                "certificate_pem": None},
        "authentication": {"ok": auth, "detail": auth_detail},
        "ibmi_version": "7.5", "qtimzon": qtimzon, "detected_timezone": zone, "timezone_ambiguous": zone is None,
    }
    return "quadringent_probe_result=" + json.dumps(document) + "\n"


def fake_admission(*, table_id: str, pipeline_id: str, raw_prefix: str = "raw") -> dict:
    identity = {"receiver_library": "QDCLIB", "receiver_name": "QSQJRN0001", "journal_library": "QDCLIB",
                "journal_name": "QSQJRN"}
    return {
        "binding": {"scope_id": "reader-source-raw-v1-" + "0" * 64},
        "boundary": {"format_version": "quadringent-journal-boundary-v2", "receiver_identity": identity,
                     "bound_checkpoint": {"sequence": 4200, "receiver_identity": identity},
                     "observed_at": "2026-10-13T09:30:00+00:00"},
        "run_id": RUN_ID,
        "evidence_key": f"{raw_prefix}/{table_id}/evidence/{RUN_ID}.json",
        "raw_prefix_root": raw_prefix,
        "stream_key": "reader-source-raw-v1-" + "0" * 64,
        "table_id": table_id,
        "pipeline_id": pipeline_id,
    }


def role_of(argv) -> str | None:
    for item in argv:
        if item.startswith("io.quadringent.local.role="):
            return item.split("=", 1)[1]
    return None


def option(argv, name):
    argv = list(argv)
    return argv[argv.index(name) + 1] if name in argv else None


def result(argv, stdout="", returncode=0, stderr="") -> CommandResult:
    return CommandResult(tuple(argv), returncode, stdout, stderr)


class DockerStub(RecordingRunner):
    """RecordingRunner that answers ``docker run`` by container role and tracks container states."""

    def __init__(self, **overrides) -> None:
        super().__init__(available_tools=frozenset({"docker", "openssl"}))
        self.handlers: dict = {}
        self.states: dict[str, str] = {}
        self.table_row = table_row()
        self.probe = probe_output()
        for role, handler in overrides.items():
            self.handlers[role.replace("_", "-")] = handler

    # -- recording helpers

    def runs(self, role: str | None = None) -> list[tuple[str, ...]]:
        found = [argv for argv, _, _ in self.invocations if argv[:2] == ("docker", "run")]
        return [argv for argv in found if role is None or role_of(argv) == role]

    def roles(self) -> list[str]:
        return [role_of(argv) for argv in self.runs()]

    # -- command answers

    def run(self, argv, *, cwd=None, env=None):
        argv = tuple(argv)
        if argv[:2] == ("docker", "run"):
            answer = self._docker_run(argv)
        elif argv[:2] == ("docker", "ps"):
            answer = result(argv, "".join(f"{name}\t{state}\n" for name, state in self.states.items()))
        elif argv[:2] == ("docker", "rm"):
            self.states.pop(argv[-1], None)
            answer = result(argv)
        elif argv[:2] == ("openssl", "genpkey"):
            Path(option(argv, "-out")).write_text("-----BEGIN PRIVATE KEY-----\nQUtFRS1LRVk=\n-----END PRIVATE KEY-----\n")
            answer = result(argv)
        elif argv[:2] == ("openssl", "pkey"):
            answer = result(argv, "-----BEGIN PUBLIC KEY-----\nQUtFRS1QVUI=\n-----END PUBLIC KEY-----\n")
        else:
            answer = result(argv, returncode=self.default_returncode)
        self.calls.append(answer)
        self.invocations.append((argv, cwd, dict(env) if env else None))
        return answer

    def _docker_run(self, argv) -> CommandResult:
        role = role_of(argv)
        handler = self.handlers.get(role)
        if handler is not None:
            answer = handler(argv) if callable(handler) else handler
            if isinstance(answer, CommandResult):
                return self._track(argv, answer)
            return self._track(argv, result(argv, answer))
        return self._track(argv, result(argv, self._default(role, argv)))

    def _track(self, argv, answer):
        if role_of(argv) in ("capture", "loader") and answer.ok:
            self.states[option(argv, "--name")] = "running"
        return CommandResult(tuple(argv), answer.returncode, answer.stdout, answer.stderr)

    def _default(self, role, argv) -> str:
        if role == "fs-probe":
            return RESULT + json.dumps({"ok": True, "checks": {"hard_link": {"ok": True, "detail": "ok"}}}) + "\n"
        if role == "probe":
            return self.probe
        if role == "discover":
            return discover_output(self.table_row)
        if role == "boundary":
            return RESULT + json.dumps({"receiver_library": "QDCLIB", "receiver_name": "QSQJRN0001",
                                        "last_sequence": 4200, "observed_at": "2026-10-13T09:30:00+00:00",
                                        "receiver_identity": {}}) + "\n"
        if role == "admit":
            admission = fake_admission(table_id=option(argv, "--table-id"), pipeline_id=option(argv, "--pipeline-id"),
                                       raw_prefix=option(argv, "--raw-prefix-root"))
            return RESULT + json.dumps(admission) + "\n"
        if role == "copy":
            return RESULT + json.dumps({"evidence_key": "raw/t/evidence/x.json", "rows": 3}) + "\n"
        if role == "verify":
            return RESULT + json.dumps({"ok": True, "checks": {"connection": {"ok": True, "detail": "ok"}}}) + "\n"
        return "container-id\n"  # detached services


class Cli:
    """One local root; helpers to invoke the real CLI parser with stubbed Docker."""

    def __init__(self, tmp_path: Path, name: str = "demo", runner: DockerStub | None = None) -> None:
        self.root = tmp_path / "local-root"
        self.name = name
        self.runner = runner or DockerStub()
        self.paths = cfg.LocalPaths(self.root, name)
        self.out, self.err = io.StringIO(), io.StringIO()

    def invoke(self, *argv: str, stdin: str | None = None, confirm=None, **kwargs) -> int:
        self.out, self.err = io.StringIO(), io.StringIO()
        base = ["local", argv[0], "--root", str(self.root), "--name", self.name, *argv[1:]]
        args = _build_parser().parse_args(base)
        return run_local_command(args, self.runner, self.out, stderr=self.err, stdin=io.StringIO(stdin or ""),
                                 uid=1000, gid=1000, confirm=confirm, **kwargs)

    def init(self, *extra: str, password: str = PASSWORD + "\n") -> int:
        return self.invoke("init", "--ibmi-host", IBMI_HOST, "--ibmi-user", "svcuser", "--table", "qdclib/orders",
                           "--snowflake-account", "exampleorg-exampleaccount", *extra, stdin=password)

    def config(self) -> cfg.LocalConfig:
        return cfg.load(self.paths)

    def env_file(self, role: str) -> dict[str, str]:
        text = self.paths.env_file(role).read_text()
        return dict(line.split("=", 1) for line in text.splitlines())

    def start(self, *extra: str, **kwargs) -> int:
        return self.invoke("start", "--yes", *extra, **kwargs)
