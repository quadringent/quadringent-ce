"""Contrat du console opérateur de checkpoint : CAS strict, audit, zéro secret."""

from __future__ import annotations

import io
import json
import os
import re
from copy import deepcopy
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

import site_fixture
import as400_checkpoint as console
from quadringent.checkpoint import DynamoDbCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity

ENV = dict(site_fixture.TEST_SITE_ENV)
ENV["AS400_STREAM_KEY"] = "ibmi/ledger/sale"


class _ConditionalFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeDynamoDb:
    """Model the installed partition key and conditional put/update calls."""

    def __init__(self, partition_key="pk") -> None:
        self.partition_key = partition_key
        self.item = None
        self.before_put = None
        self.before_update = None
        self.put_calls = []
        self.update_calls = []

    def _check_key(self, key):
        if set(key) != {self.partition_key}:
            raise ValueError("key does not match the installed DynamoDB partition key")

    def get_item(self, *, TableName, Key, ConsistentRead=False):
        self._check_key(Key)
        if self.item is None or self.item[self.partition_key] != Key[self.partition_key]:
            return {}
        return {"Item": deepcopy(self.item)}

    def _check_position(self, kwargs) -> None:
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        condition = kwargs.get("ConditionExpression")
        if condition == "attribute_not_exists(#stream)":
            assert names["#stream"] == self.partition_key
            if self.item is not None:
                raise _ConditionalFailed()
            return
        if not condition.startswith("#receiver = :receiver AND #sequence = :sequence"):
            raise AssertionError(f"unexpected condition: {condition}")
        for alias in re.findall(r"attribute_not_exists\((#[a-z_]+)\)", condition):
            if alias == "#format" and "OR" in condition:
                continue
            if names[alias] in (self.item or {}):
                raise _ConditionalFailed()
        for alias, token in re.findall(r"(#[a-z_]+) = (:[a-z_]+)", condition):
            actual = (self.item or {}).get(names[alias])
            if alias == "#format" and "OR" in condition and actual is None:
                continue
            if actual != values[token]:
                raise _ConditionalFailed()

    def put_item(self, **kwargs):
        if self.before_put is not None:
            hook, self.before_put = self.before_put, None
            hook()
        self.put_calls.append(deepcopy(kwargs))
        if self.partition_key not in kwargs["Item"]:
            raise ValueError("item does not match the installed DynamoDB partition key")
        self._check_position(kwargs)
        self.item = deepcopy(kwargs["Item"])
        return {}

    def update_item(self, **kwargs):
        self._check_key(kwargs["Key"])
        if self.before_update is not None:
            hook, self.before_update = self.before_update, None
            hook()
        self.update_calls.append(deepcopy(kwargs))
        self._check_position(kwargs)
        assert self.item is not None
        assignments = kwargs["UpdateExpression"].removeprefix("SET ").split(", ")
        values = kwargs["ExpressionAttributeValues"]
        for assignment in assignments:
            attr, placeholder = (part.strip() for part in assignment.split(" = "))
            self.item[attr] = values[placeholder]
        return {}


def _run(argv: list[str], client: _FakeDynamoDb):
    with patch.dict(os.environ, ENV, clear=True), patch("sys.argv", ["checkpoint", *argv]), patch.object(
        console, "_dynamodb_client", return_value=client
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            code = console.main()
    return code, output.getvalue()


def _seed(client: _FakeDynamoDb, receiver="DEMOJRN3866", sequence=159602291) -> None:
    client.item = {
        client.partition_key: {"S": ENV["AS400_STREAM_KEY"]},
        "format_version": {"S": "as400-checkpoint-v1"},
        "receiver": {"S": receiver},
        "sequence": {"N": str(sequence)},
    }


class CheckpointConsoleTests(unittest.TestCase):
    def test_status_prints_the_durable_position(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        code, out = _run(["status"], client)
        self.assertEqual(code, 0)
        payload = json.loads(out.strip())
        self.assertEqual(payload["state"]["receiver"], "DEMOJRN3866")
        self.assertEqual(payload["state"]["sequence"], 159602291)
        self.assertNotIn("password", out.lower())

    def test_anchor_reanchors_and_audits(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        code, out = _run(
            ["anchor", "--receiver", "DEMOJRN4115", "--sequence", "355013677",
             "--actor", "ops-marie", "--reason", "receiver purged; re-anchor to certified position"],
            client,
        )
        self.assertEqual(code, 0)
        payload = json.loads(out.strip())
        self.assertEqual(payload["action"], "anchored")
        self.assertEqual(payload["position"]["receiver"], "DEMOJRN4115")
        self.assertEqual(payload["position"]["sequence"], 355013677)
        self.assertEqual(payload["previous"]["receiver"], "DEMOJRN3866")
        self.assertEqual(client.item["receiver"], {"S": "DEMOJRN4115"})
        self.assertEqual(client.item["sequence"], {"N": "355013677"})
        self.assertEqual(client.item["anchor_actor"], {"S": "ops-marie"})
        self.assertEqual(
            client.item["anchor_reason"],
            {"S": "receiver purged; re-anchor to certified position"},
        )
        self.assertEqual(client.update_calls[-1]["Key"], {"pk": {"S": ENV["AS400_STREAM_KEY"]}})
        code, out = _run(["status"], client)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"]["receiver"], "DEMOJRN4115")

    def test_anchor_rejects_a_predecessor_that_changed_after_load(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        rival = deepcopy(client.item)
        rival["sequence"] = {"N": "159602292"}
        client.before_put = lambda: setattr(client, "item", deepcopy(rival))
        code, out = _run(
            ["anchor", "--receiver", "DEMOJRN4115", "--sequence", "355013677",
             "--actor", "ops-marie", "--reason", "re-anchor"],
            client,
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), {"event": "checkpoint_error", "error": "cas_conflict"})
        self.assertEqual(client.item, rival)
        self.assertEqual(client.update_calls, [])

    def test_status_reads_the_complete_legacy_anchor_audit(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        client.item.update({"anchor_actor": {"S": "ops-marie"},
                            "anchor_at": {"S": "2026-10-13T12:34:56+00:00"},
                            "anchor_reason": {"S": "certified position"}})
        before = deepcopy(client.item)
        code, out = _run(["status"], client)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"]["sequence"], 159602291)
        self.assertEqual(client.item, before)
        store = DynamoDbCheckpointStore("checkpoints", ENV["AS400_STREAM_KEY"], client=client)
        with self.assertRaisesRegex(ValueError, "legacy|unbound"):
            store.load_bound()

    def test_partial_or_malformed_legacy_audit_is_rejected(self) -> None:
        complete = {"anchor_actor": {"S": "ops-marie"},
                    "anchor_at": {"S": "2026-10-13T12:34:56+00:00"},
                    "anchor_reason": {"S": "certified position"}}
        malformed = []
        for field in complete:
            partial = deepcopy(complete)
            del partial[field]
            malformed.append(partial)
        for field, bad in (("anchor_actor", {"N": "1"}), ("anchor_actor", {"S": "bad actor!!"}),
                           ("anchor_at", {"S": "not-a-timestamp"}),
                           ("anchor_at", {"S": "2026-10-13T12:34:56"}),
                           ("anchor_reason", {"S": " "}), ("anchor_reason", {"S": "x" * 201})):
            invalid = deepcopy(complete)
            invalid[field] = bad
            malformed.append(invalid)
        for audit in malformed:
            with self.subTest(audit=audit):
                client = _FakeDynamoDb()
                _seed(client)
                client.item.update(audit)
                before = deepcopy(client.item)
                store = DynamoDbCheckpointStore("checkpoints", ENV["AS400_STREAM_KEY"], client=client)
                with self.assertRaises(ValueError):
                    store.load()
                self.assertEqual(client.item, before)

    def test_unknown_legacy_attribute_is_rejected(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        client.item["unexpected"] = {"S": "cannot-drop"}
        before = deepcopy(client.item)
        store = DynamoDbCheckpointStore("checkpoints", ENV["AS400_STREAM_KEY"], client=client)
        with self.assertRaises(ValueError):
            store.load()
        self.assertEqual(client.item, before)

    def test_a_bound_checkpoint_inserted_during_audit_is_unchanged(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        bound_client = _FakeDynamoDb()
        identity = ReceiverIdentity("QGPL", "JRN", "QGPL", "RCV4115", "fixture-native-v1", "opaque-generation")
        bound = BoundCheckpoint("scope", "epoch", identity, 355013677)
        DynamoDbCheckpointStore("checkpoints", ENV["AS400_STREAM_KEY"], client=bound_client).compare_and_set_bound(None, bound)
        replacement = deepcopy(bound_client.item)
        client.before_update = lambda: setattr(client, "item", deepcopy(replacement))

        code, out = _run(["anchor", "--receiver", "RCV4115", "--sequence", "355013677",
                          "--actor", "ops-marie", "--reason", "certified position"], client)

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), {"event": "checkpoint_error", "error": "audit_conflict",
                                          "position_transitioned": True})
        self.assertEqual(client.item, replacement)
        self.assertEqual(DynamoDbCheckpointStore("checkpoints", ENV["AS400_STREAM_KEY"], client=client).load_bound(), bound)

    def test_a_concurrent_cursor_change_during_audit_is_an_explicit_audit_conflict(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        rival = deepcopy(client.item)
        rival["receiver"] = {"S": "DEMOJRN4115"}
        rival["sequence"] = {"N": "355013678"}
        client.before_update = lambda: setattr(client, "item", deepcopy(rival))
        code, out = _run(["anchor", "--receiver", "DEMOJRN4115", "--sequence", "355013677",
                          "--actor", "ops-marie", "--reason", "certified position"], client)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), {"event": "checkpoint_error", "error": "audit_conflict",
                                          "position_transitioned": True})
        self.assertEqual(client.item, rival)

    def test_audit_failure_without_a_rival_reports_the_already_durable_transition(self) -> None:
        for error, code_name in ((_ConditionalFailed(), "audit_conflict"),
                                 (RuntimeError("private client detail"), "audit_failed")):
            with self.subTest(code_name=code_name):
                client = _FakeDynamoDb()
                _seed(client)

                def deny_audit(error=error):
                    raise error

                client.before_update = deny_audit
                code, out = _run(["anchor", "--receiver", "DEMOJRN4115", "--sequence", "355013677",
                                  "--actor", "ops-marie", "--reason", "certified position"], client)

                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out), {"event": "checkpoint_error", "error": code_name,
                                                  "position_transitioned": True})
                self.assertNotIn("private client detail", out)
                store = DynamoDbCheckpointStore("checkpoints", ENV["AS400_STREAM_KEY"], client=client)
                self.assertEqual(store.load(), JournalPosition("DEMOJRN4115", 355013677))

    def test_anchor_rejects_invalid_actor(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        code, out = _run(
            ["anchor", "--receiver", "DEMOJRN4115", "--sequence", "355013677",
             "--actor", "bad actor!!", "--reason", "x"],
            client,
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid_actor", out)


if __name__ == "__main__":
    unittest.main()
