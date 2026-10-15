"""Disk-backed exact comparison; collection and native admission remain separate."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .proof import normalize
from .verification_contract import DATASETS, VerificationScope
from .verification_stream import FrameReader, StreamError

FORMAT_VERSION = "quadringent-native-table-comparison-v1"
_TECHNICAL = {"EVENT_ID", "OPERATION", "JOURNAL_RECEIVER", "JOURNAL_SEQUENCE"}
_EVENTS = {"snapshot", "journal", "raw", "history"}
MAX_CANONICAL_NUMBER_CHARS = 1024


class VerificationError(ValueError):
    """A comparison could not admit a complete bounded input."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _value(value, kind):
    if value is None:
        return None
    if type(value) is bool or isinstance(value, (dict, list, float)):
        raise ValueError("inexact or unsupported value")
    if kind in {"int", "decimal"}:
        if not isinstance(value, (str, int, Decimal)):
            raise ValueError("unsupported numeric value")
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("nonfinite numeric value")
        if number.is_zero():
            return "0"
        sign, digits, exponent = number.as_tuple()
        expanded = sign + max(len(digits) + exponent, 1) + max(-exponent, 0) + 1
        if expanded > MAX_CANONICAL_NUMBER_CHARS:
            raise ValueError("canonical numeric value exceeds budget")
        return normalize(value, kind)
    if type(value) is not str:
        raise ValueError("textual native value required")
    if kind == "date":
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError("invalid date")
        date.fromisoformat(value)
    elif kind == "timestamp":
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", value) is None:
            raise ValueError("invalid timestamp")
        datetime.fromisoformat(value)
    return normalize(value, kind)


class DiskReconciler:
    """Consume all seven sealed streams before returning any equality verdict."""

    def __init__(self, directory, scope: VerificationScope, *,
                 max_database_bytes=8 * 1024**3, max_seconds=3600):
        if type(scope) is not VerificationScope:
            raise VerificationError("invalid_verification_scope")
        if (type(max_database_bytes) is not int or not 262144 <= max_database_bytes <= 64 * 1024**3
                or type(max_seconds) is not int or not 1 <= max_seconds <= 7200):
            raise VerificationError("invalid_verification_budget")
        self.directory = Path(directory)
        if any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
            raise VerificationError("unsafe_verification_directory")
        try:
            self.directory.mkdir(mode=0o700, exist_ok=False)
            os.chmod(self.directory, 0o700)
            path = self.directory / "ledger.sqlite3"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self._db = sqlite3.connect(path)
            self._db.execute("PRAGMA page_size=4096")
            self._db.execute(f"PRAGMA max_page_count={max_database_bytes // 4096}")
            self._db.execute("PRAGMA cache_size=-8192")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA temp_store=FILE")
            self._db.executescript("""
                CREATE TABLE records (
                    dataset TEXT NOT NULL, identity TEXT NOT NULL, image TEXT NOT NULL,
                    payload TEXT NOT NULL, row_key TEXT, receiver_rank INTEGER,
                    sequence TEXT, operation TEXT, position_key TEXT,
                    PRIMARY KEY (dataset, identity),
                    UNIQUE (dataset, position_key)
                ) WITHOUT ROWID;
                CREATE INDEX journal_order ON records(receiver_rank,length(sequence),sequence)
                    WHERE dataset='journal';
                CREATE TABLE expected_mirror (identity TEXT PRIMARY KEY, payload TEXT NOT NULL) WITHOUT ROWID;
            """)
        except (OSError, sqlite3.Error):
            raise VerificationError("verification_storage_unavailable") from None
        self.scope = scope
        self._deadline = time.monotonic() + max_seconds
        self._db.set_progress_handler(lambda: int(time.monotonic() > self._deadline), 10000)
        self._summaries = {}
        self._attempted = set()
        self._failures = {}
        self._finished = None
        self._closed = False
        self._windows = {w.identity.receiver_name: (i, w) for i, w in enumerate(scope.receiver_windows)}
        self._columns = {c.name: c.kind for c in scope.columns}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if not self._closed:
            self._db.close()
            self._closed = True

    def _admit_open(self):
        if self._closed or self._finished is not None:
            raise VerificationError("verification_already_closed")
        if time.monotonic() > self._deadline:
            raise VerificationError("verification_budget_exceeded")

    def _position(self, row):
        receiver, raw_sequence = row["JOURNAL_RECEIVER"], row["JOURNAL_SEQUENCE"]
        if type(receiver) is not str or type(raw_sequence) not in (str, int):
            raise ValueError("invalid position")
        sequence = str(raw_sequence)
        if re.fullmatch(r"0|[1-9][0-9]*", sequence) is None:
            raise ValueError("noncanonical sequence")
        return receiver, sequence

    def _record(self, dataset, row):
        if type(row) is not dict:
            raise ValueError("row object required")
        if dataset == "positions":
            if set(row) != {"JOURNAL_RECEIVER", "JOURNAL_SEQUENCE"}:
                raise ValueError("position columns differ")
            receiver, sequence = self._position(row)
            rank, window = self._windows[receiver]
            if not window.start_sequence <= int(sequence) <= window.end_sequence:
                raise ValueError("source position outside admitted window")
            key = _json([receiver, sequence])
            return (dataset, key, key, key, None, rank, sequence, None, key)
        expected = set(self._columns) | (_TECHNICAL if dataset in _EVENTS else set())
        if set(row) != expected:
            raise ValueError("columns differ")
        values = [_value(row[c.name], c.kind) for c in self.scope.columns]
        clean = dict(zip(self._columns, values, strict=True))
        primary = [clean[name] for name in self.scope.primary_key]
        if any(x is None for x in primary):
            raise ValueError("null primary key")
        key, image = _json(primary), _json(values)
        if dataset not in _EVENTS:
            return (dataset, key, image, image, key, None, None, None, None)
        receiver, sequence = self._position(row)
        operation = row["OPERATION"]
        if operation not in {"c", "u_before", "u_after", "d"}:
            raise ValueError("unknown native operation")
        snapshot = receiver == "SNAPSHOT:" + self.scope.copy_run_id
        if snapshot:
            if dataset == "journal" or operation != "c" or not 1 <= int(sequence) <= self.scope.expected_snapshot_rows:
                raise ValueError("snapshot epoch or ordinal differs")
            rank = -1
            journal = "SNAPSHOT:" + self.scope.source_library + "." + self.scope.source_table
        else:
            if dataset == "snapshot":
                raise ValueError("native event in snapshot")
            rank, window = self._windows[receiver]
            if not window.start_sequence <= int(sequence) <= window.end_sequence:
                raise ValueError("event outside admitted window")
            journal = self.scope.capture_binding.journal
        identity = hashlib.sha256(f"ibmi|{journal}|{receiver}|{sequence}".encode()).hexdigest()
        if row["EVENT_ID"] != identity:
            raise ValueError("event identity differs")
        payload = _json([values, operation, receiver, sequence])
        return (dataset, identity, image, payload, key, rank, sequence, operation, _json([receiver, sequence]))

    def ingest(self, dataset, reader: FrameReader):
        self._admit_open()
        if dataset not in DATASETS or dataset in self._attempted:
            raise VerificationError("invalid_or_repeated_dataset")
        self._attempted.add(dataset)
        if not isinstance(reader, FrameReader) or reader.dataset != dataset or reader.scope_sha256 != self.scope.sha256:
            self._failures[dataset] = "stream_scope_mismatch"
            raise VerificationError("stream_scope_mismatch")
        duplicates = conflicts = replays = consumed = 0
        self._db.execute("SAVEPOINT ingest")
        try:
            for row in reader:
                if consumed % 256 == 0:
                    self._admit_open()
                record = self._record(dataset, row)
                consumed += 1
                try:
                    self._db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)", record)
                except sqlite3.IntegrityError:
                    duplicates += 1
                    previous = self._db.execute(
                        "SELECT payload FROM records WHERE dataset=? AND identity=?", (dataset, record[1])
                    ).fetchone()
                    if previous is None or previous[0] != record[3]:
                        conflicts += 1
                    elif dataset == "raw":
                        replays += 1
            summary = reader.summary
            if summary is None or summary.row_count != consumed:
                raise VerificationError("complete_stream_end_missing")
            unique = self._db.execute("SELECT count(*) FROM records WHERE dataset=?", (dataset,)).fetchone()[0]
            self._db.execute("RELEASE ingest")
            self._db.commit()
            self._summaries[dataset] = {
                **asdict(summary), "unique_count": unique, "duplicate_count": duplicates,
                "conflicting_identity_count": conflicts, "exact_replay_count": replays,
            }
        except (ValueError, KeyError, TypeError, ArithmeticError, StreamError, sqlite3.Error):
            try:
                self._db.execute("ROLLBACK TO ingest")
                self._db.execute("RELEASE ingest")
            except sqlite3.Error:
                pass
            self._failures[dataset] = "invalid_or_incomplete_stream"
            raise VerificationError("invalid_or_incomplete_stream") from None

    def _compare(self, expected, actual):
        # Only internal constant queries reach these SQL fragments.
        left = f"({expected})"
        right = f"({actual})"
        expected_count = self._db.execute(f"SELECT count(*) FROM {left}").fetchone()[0]
        actual_count = self._db.execute(f"SELECT count(*) FROM {right}").fetchone()[0]
        missing = self._db.execute(
            f"SELECT count(*) FROM {left} e LEFT JOIN {right} a ON a.identity=e.identity WHERE a.identity IS NULL"
        ).fetchone()[0]
        unexpected = self._db.execute(
            f"SELECT count(*) FROM {right} a LEFT JOIN {left} e ON e.identity=a.identity WHERE e.identity IS NULL"
        ).fetchone()[0]
        different = self._db.execute(
            f"SELECT count(*) FROM {left} e JOIN {right} a ON e.identity=a.identity WHERE e.payload!=a.payload"
        ).fetchone()[0]
        return {"expected_count": expected_count, "actual_count": actual_count, "missing_count": missing,
                "unexpected_count": unexpected, "different_count": different,
                "equal": missing == unexpected == different == 0}

    def _fold(self):
        consistent = True
        outstanding_before = 0
        for index, (key, image) in enumerate(self._db.execute("SELECT row_key,image FROM records WHERE dataset='snapshot'")):
            if index % 256 == 0:
                self._admit_open()
            try:
                self._db.execute("INSERT INTO expected_mirror VALUES (?,?)", (key, image))
            except sqlite3.IntegrityError:
                consistent = False
        journal = self._db.execute(
            "SELECT row_key,image,operation FROM records WHERE dataset='journal' "
            "ORDER BY receiver_rank,length(sequence),sequence"
        )
        for index, (key, image, operation) in enumerate(journal):
            if index % 256 == 0:
                self._admit_open()
            current = self._db.execute("SELECT payload FROM expected_mirror WHERE identity=?", (key,)).fetchone()
            if operation == "u_before":
                if current != (image,):
                    consistent = False
                self._db.execute("DELETE FROM expected_mirror WHERE identity=?", (key,))
                outstanding_before += 1
            elif operation == "u_after":
                if current is not None or outstanding_before == 0:
                    consistent = False
                if outstanding_before:
                    outstanding_before -= 1
                self._db.execute("INSERT INTO expected_mirror VALUES (?,?) "
                                 "ON CONFLICT(identity) DO UPDATE SET payload=excluded.payload", (key, image))
            elif operation == "c":
                if current is not None:
                    consistent = False
                self._db.execute("INSERT INTO expected_mirror VALUES (?,?) "
                                 "ON CONFLICT(identity) DO UPDATE SET payload=excluded.payload", (key, image))
            else:
                if current != (image,):
                    consistent = False
                self._db.execute("DELETE FROM expected_mirror WHERE identity=?", (key,))
        # Apply row images; do not infer an update pairing or transaction identity.
        return consistent and outstanding_before == 0

    def finish(self):
        self._admit_open()
        report = {
            "format_version": FORMAT_VERSION, "scope_sha256": self.scope.sha256,
            "state": "incomplete", "datasets": {x: self._summaries.get(x) for x in DATASETS},
            "comparisons": None, "checks": None, "ignored_fields": 0,
            "failure_codes": dict(self._failures),
            "equality_scope": "complete_layer_contents_and_reconstructed_final_state",
            "update_pairing_verified": False, "transaction_consistency_verified": False,
            "numeric_value_max_characters": MAX_CANONICAL_NUMBER_CHARS,
        }
        if self._failures or set(self._summaries) != set(DATASETS):
            self._finished = report
            return report
        try:
            transitions = self._fold()
            expected_history = "SELECT identity,payload FROM records WHERE dataset IN ('snapshot','journal')"
            comparisons = {
                "journal_positions": self._compare(
                    "SELECT identity,payload FROM records WHERE dataset='positions'",
                    "SELECT position_key AS identity,position_key AS payload FROM records WHERE dataset='journal'"),
                "raw": self._compare(expected_history,
                                     "SELECT identity,payload FROM records WHERE dataset='raw'"),
                "history": self._compare(expected_history,
                                         "SELECT identity,payload FROM records WHERE dataset='history'"),
                "source": self._compare("SELECT identity,payload FROM expected_mirror",
                                        "SELECT identity,payload FROM records WHERE dataset='source'"),
                "mirror": self._compare("SELECT identity,payload FROM records WHERE dataset='source'",
                                        "SELECT identity,payload FROM records WHERE dataset='mirror'"),
            }
            checks = {
                "snapshot_rows_complete": self._summaries["snapshot"]["unique_count"] == self.scope.expected_snapshot_rows,
                "journal_row_images_reconstructed": transitions,
                "all_streams_complete": True, "all_fields_compared": True,
                "no_logical_duplicates": all(
                    x["duplicate_count"] == 0 if name != "raw" else x["conflicting_identity_count"] == 0
                    for name, x in self._summaries.items()
                ),
            }
            report.update({"state": "verified" if all(checks.values()) and all(c["equal"] for c in comparisons.values())
                           else "divergent", "comparisons": comparisons, "checks": checks})
            self._db.commit()
            descriptor = os.open(self.directory / "ledger.sqlite3", os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._admit_open()
        except (OSError, sqlite3.Error, VerificationError):
            self._db.rollback()
            report.update(state="incomplete", comparisons=None, checks=None)
            report["failure_codes"]["comparison"] = "verification_storage_or_deadline_exceeded"
        self._finished = report
        return report

