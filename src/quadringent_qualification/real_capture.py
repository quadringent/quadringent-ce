"""Copie initiale et capture bornée avec l'image immuable du produit.

Le mot de passe IBM i entre dans le conteneur par stdin, puis dans
``ISERIES_PASSWORD`` seulement à l'intérieur du processus éphémère. Docker
ne reçoit ni le secret dans ses arguments ni une clé cloud statique. Le runner
Linux doit disposer d'une identité de VM autorisée sur le bucket et l'état.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import queue
import threading
import time
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from quadringent.storage_layout import journal_prefix
from quadringent_control_plane.v2.executor.evidence import evidence_key

from .adapters import CaptureBoundary, CaptureResult
from .config import RunConfig
from .real_source import _load_credential


_IMAGE_DIGEST = re.compile(r"(?:\S+@)?sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_CAPTURE_LABEL = re.compile(r"capture-[1-9][0-9]*\Z")
_PROCESS_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
_PASSWORD_STDIN_WRAPPER = (
    'IFS= read -r ISERIES_PASSWORD || exit 64; '
    'export ISERIES_PASSWORD; exec /opt/venv/bin/python "$@"'
)
_MAX_OUTPUT_BYTES = 1024 * 1024


class DockerCaptureRunner:
    """``CaptureRunner`` avec un checkpoint et un préfixe propres au run."""

    def __init__(self, config: RunConfig, *, runner: Callable[..., Any] = subprocess.run,
                 process_factory: Callable[..., Any] = subprocess.Popen,
                 diagnostic_directory: Path | None = None) -> None:
        if _IMAGE_DIGEST.fullmatch(config.capture.image) is None:
            raise ValueError("qualification capture image must be pinned by digest")
        try:
            if str(UUID(config.run_id)) != config.run_id:
                raise ValueError
        except ValueError:
            raise ValueError("qualification capture run_id must be a canonical UUID") from None
        library, table = config.table.qualified_name.split(".", 1)
        if any(_IDENTIFIER.fullmatch(value) is None for value in (
            library, table, config.source.journal_library, config.source.journal_name,
        )):
            raise ValueError("qualification capture source identifier is invalid")
        prefix = config.storage.raw_prefix
        path = PurePosixPath(prefix)
        if (not prefix or "\\" in prefix or any(ord(char) < 32 for char in prefix)
                or not path.parts or path.is_absolute() or ".." in path.parts or str(path) != prefix
                or path.parts[-1] != config.run_id):
            raise ValueError("qualification capture run prefix must end with run_id")
        if config.storage.backend not in {"s3", "gcs"}:
            raise ValueError("qualification capture storage backend is invalid")
        if not config.storage.bucket or not config.storage.checkpoint_location:
            raise ValueError("qualification capture bucket and checkpoint location are required")
        if config.capture.max_seconds <= 15 or config.capture.max_consecutive_errors < 1:
            raise ValueError("qualification capture time and error budgets are invalid")
        time_zone = config.source.source_time_zone
        if not isinstance(time_zone, str) or not time_zone:
            raise ValueError("qualification source time zone is required")
        try:
            ZoneInfo(time_zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("qualification source time zone is invalid") from None

        self.config = config
        self._library = library
        self._table = table
        self._table_id = f"qual-{config.run_id}"
        self._runner = runner
        self._process_factory = process_factory
        self._warm: Any = None
        self._warm_lines: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=128)
        self._warm_name = ""
        self._warm_env: dict[str, str] = {}
        self._warm_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail = ""
        self._diagnostic_directory = diagnostic_directory or Path.cwd() / ".quadringent-capture-diagnostics"
        self._diagnostic_records: list[dict[str, Any]] = []
        self.diagnostic_error: str | None = None
        self._credential = _load_credential(
            config.source.connection_secret_file,
            configured_host=config.source.host,
            configured_user=config.source.user,
        )

    def run(self, *, label: str, max_seconds: int, bootstrap: CaptureBoundary | None,
            env: Mapping[str, str]) -> CaptureResult:
        return self._run(label=label, max_seconds=max_seconds, bootstrap=bootstrap, env=env)

    def prepare_freshness(self, *, bootstrap: CaptureBoundary) -> CaptureResult:
        """Un poll achevé (y compris au tail) prouve la session avant DML."""
        if self._warm is not None:
            raise ValueError("freshness worker already exists")
        result = self._run(label="capture-1", max_seconds=self.config.capture.max_seconds,
                           bootstrap=bootstrap, env={}, prepare=True)
        if result.exit_code:
            return result
        return self._wait_warm_poll(self.config.capture.max_seconds)

    def _pump_stderr(self) -> None:
        process = self._warm
        for chunk in iter(lambda: process.stderr.read(4096), ""):
            self._stderr_tail = (self._stderr_tail + chunk)[-16384:]

    def _exited_result(self) -> CaptureResult:
        try:
            code = self._warm.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return CaptureResult(124, (), "capture_worker_output_closed", 0)
        return CaptureResult(int(code), (), "capture_worker_exited", 0)

    @staticmethod
    def _completed_idle(item: dict[str, Any], metrics: dict[str, Any]) -> bool:
        tail = metrics.get("last_source_tail")
        return (item.get("status") == "idle" and item.get("window") is None
                and item.get("event_count") == 0 and type(metrics.get("idle_polls")) is int
                and metrics["idle_polls"] > 0 and type(metrics.get("last_lag_sequences")) is int
                and metrics["last_lag_sequences"] == 0
                and isinstance(tail, dict) and isinstance(tail.get("receiver"), str)
                and bool(tail["receiver"]) and type(tail.get("sequence")) is int
                and tail["sequence"] >= 1)

    def _pump_warm(self) -> None:
        try:
            for line in self._warm.stdout:
                if len(line) > 65536:
                    break
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict) and item.get("event") == "capture_poll":
                    self._diagnostic_records = (self._diagnostic_records + [{
                        "event": "capture_poll", "status": item.get("status")
                        if item.get("status") in {"idle", "empty_scan", "published", "error"} else "unknown",
                        "polls": item["metrics"]["polls"]
                        if isinstance(item.get("metrics"), dict)
                        and type(item["metrics"].get("polls")) is int else None,
                    }])[-16:]
                    self._warm_lines.put(item, timeout=1)
        except (OSError, queue.Full):
            pass
        finally:
            try:
                self._warm_lines.put(None, timeout=1)
            except queue.Full:
                pass

    def _wait_warm_poll(self, timeout: float) -> CaptureResult:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._warm is None:
                return CaptureResult(125, (), "capture_worker_exited", 0)
            returncode = self._warm.poll()
            if returncode is not None:
                return CaptureResult(int(returncode), (), "capture_worker_exited", 0)
            try:
                item = self._warm_lines.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                return CaptureResult(124, (), "capture_timeout", 0)
            if item is None:
                return self._exited_result()
            metrics = item.get("metrics")
            count = item.get("event_count")
            if (isinstance(metrics, dict)
                    and (item.get("status") in {"published", "empty_scan"}
                         or self._completed_idle(item, metrics)) and type(metrics.get("polls")) is int
                    and metrics["polls"] > 0 and type(count) is int and count >= 0):
                return CaptureResult(0, (), "capture_poll_completed", count)
            if item.get("status") not in {"idle", "published", "empty_scan"}:
                return CaptureResult(6, (), "capture_poll_failed", 0)
        return CaptureResult(124, (), "capture_timeout", 0)

    def check_freshness(self) -> CaptureResult:
        if self._warm is None:
            return CaptureResult(125, (), "capture_worker_exited", 0)
        returncode = self._warm.poll()
        if returncode is not None:
            return CaptureResult(int(returncode), (), "capture_worker_exited", 0)
        if self._warm_thread is None or not self._warm_thread.is_alive():
            return self._exited_result()
        # Inspect without consuming evidence needed by the measured poll waiter.
        with self._warm_lines.mutex:
            records = tuple(self._warm_lines.queue)
        for record in records:
            if record is None:
                return self._exited_result()
            if record.get("status") not in {"idle", "published", "empty_scan"}:
                return CaptureResult(6, (), "capture_poll_failed", 0)
        return CaptureResult(0, (), "capture_worker_live", 0)

    def close_freshness(self) -> None:
        if self._warm is None:
            return
        process = self._warm
        try:
            self._cleanup_container(self._warm_name, self._warm_env)
        finally:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if self._warm_thread is not None:
                self._warm_thread.join(timeout=2)
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=2)
            try:
                directory = self._diagnostic_directory
                if directory.is_symlink():
                    raise ValueError("private diagnostic directory is a symlink")
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if directory.stat().st_mode & 0o077:
                    raise ValueError("private diagnostic directory is not private")
                path = directory / (self._warm_name + ".private.json")
                text = self._stderr_tail.replace(self._credential.password, "[REDACTED]")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump({"process_exit_code": process.poll(), "stderr_tail": text,
                               "stderr_limit_bytes": 65536, "polls": self._diagnostic_records}, stream)
            except (OSError, ValueError) as error:
                # Le diagnostic privé ne masque jamais une erreur de nettoyage.
                self.diagnostic_error = type(error).__name__
            finally:
                self._warm = None

    def run_freshness(self, *, label: str, bootstrap: CaptureBoundary,
                      timeout_seconds: float) -> CaptureResult:
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= self.config.capture.max_seconds
                or _CAPTURE_LABEL.fullmatch(label) is None):
            raise ValueError("qualification freshness capture budget is invalid")
        if self._warm is not None:
            # A pre-write poll cannot certify a marker: the orchestrator still
            # requires its authentic RAW receipt. Never discard an error record.
            return self._wait_warm_poll(timeout_seconds)
        return self._run(label=label, max_seconds=self.config.capture.max_seconds,
                         bootstrap=bootstrap, env={}, freshness_timeout=timeout_seconds)

    def _run(self, *, label: str, max_seconds: int, bootstrap: CaptureBoundary | None,
             env: Mapping[str, str], freshness_timeout: float | None = None, prepare: bool = False) -> CaptureResult:
        if (label != "snapshot" and _CAPTURE_LABEL.fullmatch(label) is None
                or type(max_seconds) is not int or max_seconds <= 15
                or max_seconds > self.config.capture.max_seconds
                or not isinstance(bootstrap, CaptureBoundary) or env):
            raise ValueError("qualification capture invocation is invalid")

        snapshot = label == "snapshot"
        timeout_seconds = 15 if freshness_timeout is not None else min(max_seconds - 1, 30)
        container_env = self._container_env(bootstrap, snapshot=snapshot, timeout_seconds=timeout_seconds)
        script = "/app/quadringent_initial_copy_job.py" if snapshot else "/app/as400_continuous_capture.py"
        script_args = [] if snapshot else [
            "--max-seconds", str(max_seconds),
            "--max-consecutive-errors", str(self.config.capture.max_consecutive_errors),
            "--metrics-interval-seconds", "0" if prepare else "10",
        ]
        if freshness_timeout is not None:
            # Une lecture suffit à tenter le marqueur. La durée générale de
            # rattrapage ne doit pas retarder le chargement SQL du miroir.
            script_args = ["--max-polls", "1", "--max-consecutive-errors", "1",
                           "--metrics-interval-seconds", "10"]
        container_name = f"qdt-qual-{self.config.run_id[:8]}-{label}-{uuid4().hex[:8]}"
        command = [
            "docker", "run", "--rm", "--name", container_name,
            "--interactive", "--pull=never", "--read-only",
            "--network", "host", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
            "--pids-limit", "128", "--memory", "1g", "--cpus", "1",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        ]
        process_env = {key: os.environ[key] for key in _PROCESS_ENV if key in os.environ}
        process_env.update(container_env)
        for name in sorted(container_env):
            command.extend(("--env", name))
        command.extend((
            "--entrypoint", "/bin/sh", self.config.capture.image,
            "-c", _PASSWORD_STDIN_WRAPPER, "qualification-capture", script, *script_args,
        ))
        if prepare:
            self._warm_name, self._warm_env = container_name, process_env
            try:
                self._warm = self._process_factory(
                    command, env=process_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                self._warm.stdin.write(self._credential.password + "\n")
                self._warm.stdin.close()
                self._warm_thread = threading.Thread(target=self._pump_warm, daemon=True)
                self._warm_thread.start()
                if getattr(self._warm, "stderr", None) is not None:
                    self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
                    self._stderr_thread.start()
            except OSError:
                self.close_freshness()
                return CaptureResult(127, (), "capture_os_error", 0)
            except BaseException:
                self.close_freshness()
                raise
            return CaptureResult(0, (), "capture_worker_started", 0)
        try:
            completed = self._runner(
                command, input=self._credential.password + "\n", env=process_env,
                text=True, capture_output=True, check=False,
                timeout=(freshness_timeout if freshness_timeout is not None
                         else max_seconds + timeout_seconds + 60),
            )
        except subprocess.TimeoutExpired:
            self._cleanup_container(container_name, process_env)
            return CaptureResult(124, (), "capture_timeout", 0)
        except OSError:
            self._cleanup_container(container_name, process_env)
            return CaptureResult(127, (), "capture_os_error", 0)
        except BaseException:
            self._cleanup_container(container_name, process_env)
            raise
        if completed.returncode != 0:
            self._cleanup_container(container_name, process_env)
            return CaptureResult(exit_code=int(completed.returncode), events=(), log="capture_failed", observed_count=0)
        count = self._receipt_count(completed.stdout or "", snapshot=snapshot)
        if count is None:
            return CaptureResult(exit_code=6, events=(), log="capture_receipt_invalid", observed_count=0)
        return CaptureResult(exit_code=0, events=(), log="capture_receipt_valid", observed_count=count)

    def _cleanup_container(self, name: str, process_env: Mapping[str, str]) -> None:
        """Un délai côté client ne doit pas laisser la capture tourner seule."""
        try:
            self._runner(
                ["docker", "rm", "--force", "--volumes", name],
                env=dict(process_env), text=True, capture_output=True,
                check=False, timeout=15,
            )
            inspected = self._runner(
                ["docker", "container", "inspect", name],
                env=dict(process_env), text=True, capture_output=True,
                check=False, timeout=15,
            )
            if inspected.returncode != 0 and any(
                marker in (inspected.stderr or "").casefold()
                for marker in ("no such object", "no such container")
            ):
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise RuntimeError("qualification Docker container cleanup could not be verified")

    def _container_env(self, boundary: CaptureBoundary, *, snapshot: bool,
                       timeout_seconds: int) -> dict[str, str]:
        config = self.config
        root = config.storage.raw_prefix
        values = {
            "ISERIES_HOST": self._credential.host,
            "ISERIES_USER": self._credential.user,
            "ISERIES_SCHEMA": self._library,
            "ISERIES_TABLE": self._table,
            "AS400_TLS": "true",
            "AS400_SOURCE_TIME_ZONE": config.source.source_time_zone or "",
            "AS400_JOURNAL_LIBRARY": config.source.journal_library,
            "AS400_JOURNAL_NAME": config.source.journal_name,
            "AS400_BOOTSTRAP_RECEIVER": boundary.receiver_name,
            "AS400_BOOTSTRAP_RECEIVER_LIBRARY": boundary.receiver_library,
            "AS400_BOOTSTRAP_SEQUENCE": str(boundary.last_sequence if snapshot else boundary.next_sequence),
            "AS400_BOOTSTRAP_OBSERVED_AT": boundary.observed_at.isoformat(),
            "AS400_RAW_BUCKET": config.storage.bucket,
            "AS400_RAW_PREFIX": root if snapshot else journal_prefix(root, self._table),
            "QUADRINGENT_STORAGE_BACKEND": "gcs" if config.storage.backend == "gcs" else "aws",
        }
        if config.storage.backend == "s3":
            # Région du runner personnel, transmise par nom Docker sans credentials.
            region = os.environ.get("AWS_DEFAULT_REGION")
            if region is None:
                region = os.environ.get("AWS_REGION")
            if region is not None:
                if (len(region) > 64
                        or re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]+)+-[0-9]+", region) is None):
                    raise ValueError("qualification AWS region is invalid")
                values["AWS_DEFAULT_REGION"] = region
            # Sans valeur explicite, garder la résolution boto3 ; aucun défaut inventé.
        if snapshot:
            values.update({
                "AS400_SNAPSHOT_RUN_ID": config.run_id,
                "AS400_SNAPSHOT_OUTPUT_DIR": "/tmp/quadringent-snapshot",
                "AS400_EVIDENCE_KEY": evidence_key(root, self._table_id, config.run_id),
                "AS400_PIPELINE_ID": self._table_id,
                "AS400_TABLE_ID": self._table_id,
            })
        else:
            stream = f"qual-{config.run_id}-{config.source.journal_library}.{config.source.journal_name}"
            values.update({
                "AS400_STREAM_KEY": stream.lower(),
                "AS400_READER_TIMEOUT_SECONDS": str(timeout_seconds),
                "AS400_RETRIEVE_TIMEOUT_MS": "10000",
                "AS400_RECEIPTED_SCANS": "true",
                "AS400_POLL_SECONDS": "1",
                "AS400_MIN_POLL_SECONDS": "1",
                "AS400_CHECKPOINT_TABLE" if config.storage.backend == "s3" else "AS400_CHECKPOINT_BUCKET": (
                    config.storage.checkpoint_location or ""
                ),
            })
        return values

    def _receipt_count(self, stdout: str, *, snapshot: bool) -> int | None:
        if len(stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            return None
        expected = "initial_copy_completed" if snapshot else "capture_finished"
        receipts: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, dict):
                return None
            if parsed.get("event") == expected:
                receipts.append(parsed)
        if len(receipts) != 1:
            return None
        receipt = receipts[0]
        if snapshot:
            if receipt.get("run_id") != self.config.run_id or receipt.get("table_id") != self._table_id:
                return None
            count = receipt.get("rows_copied")
        else:
            metrics = receipt.get("metrics")
            if not isinstance(metrics, dict):
                return None
            count = metrics.get("events_published")
        return count if type(count) is int and count >= 0 else None
