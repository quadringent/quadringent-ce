"""Races between real processes sharing one local store.

Each child process builds its own ``StorageBackend.from_environment`` instance, so
nothing but the filesystem is shared: hard-link create-if-absent, ``flock`` and
atomic rename must carry the same guarantees as the S3 and DynamoDB conditions.
"""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quadringent.storage_backend import StorageBackend

from local_storage_fakes import bound_point, child_environment, init_data_dir

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "local_two_process_worker.py"
PROCESSES = 4
ROUNDS = 25


def storage_environment(tmp_path):
    return init_data_dir(tmp_path / "data")


def run_children(tmp_path, specs, *, extra_env=None, timeout=120):
    """Start every child, release them together once all are ready, return their JSON lines."""
    storage_env = storage_environment(tmp_path)
    go = tmp_path / "go"
    environment = child_environment(ROOT, storage_env)
    environment["LOCAL_TEST_GO"] = str(go)
    environment.update(extra_env or {})
    children = [subprocess.Popen([sys.executable, str(WORKER), *map(str, spec)], cwd=ROOT, env=environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for spec in specs]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "READY", child.stderr.read()
        go.write_text("go")
        outputs = []
        for child in children:
            out, err = child.communicate(timeout=timeout)
            assert child.returncode == 0, err
            outputs.append(json.loads(out.strip().splitlines()[-1]))
        return outputs
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()


def backend(tmp_path):
    return StorageBackend.from_environment(storage_environment(tmp_path))


def test_put_once_same_content_has_exactly_one_creator(tmp_path):
    outputs = run_children(tmp_path, [("put-same", index, ROUNDS) for index in range(PROCESSES)])
    for round_number in range(ROUNDS):
        assert sum(1 for results in outputs if results[round_number] is True) == 1, round_number
    store = backend(tmp_path).object_store("race")
    assert store.get("same-0.bin") == b"identical content " * 4096


def test_put_once_distinct_content_has_one_winner_and_clean_collisions(tmp_path):
    outputs = run_children(tmp_path, [("put-distinct", index, ROUNDS) for index in range(PROCESSES)])
    store = backend(tmp_path).object_store("race")
    for round_number in range(ROUNDS):
        column = [results[round_number] for results in outputs]
        assert column.count("won") == 1, column
        assert column.count("collision") == PROCESSES - 1, column
        winner = column.index("won")
        assert store.get(f"distinct-{round_number}.bin") == f"writer-{winner}".encode() * 4096


def test_compare_and_set_bound_from_nothing_has_exactly_one_success(tmp_path):
    outputs = run_children(tmp_path, [("cas-first", index, ROUNDS) for index in range(PROCESSES)])
    storage = backend(tmp_path)
    for round_number in range(ROUNDS):
        column = [results[round_number] for results in outputs]
        assert column.count("ok") == 1 and column.count("conflict") == PROCESSES - 1, column
        stored = storage.checkpoint_store(f"stream-{round_number}").load_bound()
        assert stored == bound_point(column.index("ok"))


def test_concurrent_increments_lose_no_update(tmp_path):
    storage = backend(tmp_path)
    storage.checkpoint_store("counter").compare_and_set_bound(None, bound_point(0))
    per_process = 40
    run_children(tmp_path, [("increment", index, per_process) for index in range(PROCESSES)])
    assert storage.checkpoint_store("counter").load_bound().sequence == PROCESSES * per_process


def test_reader_never_sees_a_partial_object_or_torn_checkpoint(tmp_path):
    count, size = 30, 2 * 1024 * 1024
    outputs = run_children(
        tmp_path, [("writer", 0, count), ("reader", 1, count), ("reader", 2, count)],
        extra_env={"LOCAL_TEST_SIZE": str(size)})
    for reader in outputs[1:]:
        assert reader[0]["seen"] == count
        assert reader[0]["partial"] == 0
        assert reader[0]["torn_checkpoint"] == 0
    assert backend(tmp_path).checkpoint_store("progress").load_bound().sequence == count - 1


def test_independent_instances_share_state_through_the_directory(tmp_path):
    first, second = backend(tmp_path), backend(tmp_path)
    assert first is not second
    assert first.object_store("a/b").put_once("k.json", b"{}") is True
    assert second.object_store("a/b").put_once("k.json", b"{}") is False
    first.checkpoint_store("s").compare_and_set_bound(None, bound_point(3))
    assert second.checkpoint_store("s").load_bound() == bound_point(3)
    with pytest.raises(RuntimeError, match="compare-and-set conflict"):
        second.checkpoint_store("s").compare_and_set_bound(None, bound_point(4))
