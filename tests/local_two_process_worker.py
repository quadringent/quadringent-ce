"""Child process of tests/test_local_storage_two_process.py.

Every child builds its own StorageBackend from the environment, waits for the
start file so all children contend at the same moment, then prints one JSON line.
"""
import hashlib
import json
import os
import sys
import time

from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.storage_backend import StorageBackend

IDENTITY = ReceiverIdentity("TESTLIB", "TESTJRN", "TESTLIB", "RCV0001", "native-test-v1", "generation-a")


def point(sequence):
    return BoundCheckpoint("scope-1", "epoch-1", IDENTITY, sequence)


def wait_for_start():
    print("READY", flush=True)
    deadline = time.time() + 60
    while not os.path.exists(os.environ["LOCAL_TEST_GO"]):
        if time.time() > deadline:
            raise SystemExit("start file never appeared")
        time.sleep(0.001)


def payload(index, size):
    block = hashlib.sha256(str(index).encode()).digest()
    return block * (size // len(block))


def main():
    mode, identity, rounds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    storage = StorageBackend.from_environment(os.environ)
    wait_for_start()
    results = []
    if mode == "put-same":
        store = storage.object_store("race")
        for round_number in range(rounds):
            results.append(store.put_once(f"same-{round_number}.bin", b"identical content " * 4096))
    elif mode == "put-distinct":
        store = storage.object_store("race")
        for round_number in range(rounds):
            try:
                results.append("won" if store.put_once(f"distinct-{round_number}.bin",
                                                      f"writer-{identity}".encode() * 4096) else "same")
            except ValueError as error:
                results.append("collision" if "object collision" in str(error) else f"other:{error}")
    elif mode == "cas-first":
        for round_number in range(rounds):
            checkpoint = storage.checkpoint_store(f"stream-{round_number}")
            try:
                checkpoint.compare_and_set_bound(None, point(identity))
                results.append("ok")
            except RuntimeError as error:
                results.append("conflict" if "compare-and-set conflict" in str(error) else f"other:{error}")
    elif mode == "increment":
        checkpoint = storage.checkpoint_store("counter")
        for _ in range(rounds):
            while True:
                current = checkpoint.load_bound()
                try:
                    checkpoint.compare_and_set_bound(current, point(current.sequence + 1))
                    break
                except RuntimeError:
                    continue
        results.append(rounds)
    elif mode == "writer":
        store = storage.object_store("parts")
        size = int(os.environ["LOCAL_TEST_SIZE"])
        for round_number in range(rounds):
            store.put_once(f"{round_number:03d}.bin", payload(round_number, size))
            checkpoint = storage.checkpoint_store("progress")
            current = checkpoint.load_bound()
            checkpoint.compare_and_set_bound(current, point(round_number))
        results.append(rounds)
    elif mode == "reader":
        store = storage.object_store("parts")
        checkpoint = storage.checkpoint_store("progress")
        size = int(os.environ["LOCAL_TEST_SIZE"])
        seen, partial, torn_checkpoint = set(), 0, 0
        deadline = time.time() + 60
        while len(seen) < rounds and time.time() < deadline:
            try:
                checkpoint.load_bound()
            except Exception:
                torn_checkpoint += 1
            for round_number in range(rounds):
                if round_number in seen:
                    continue
                try:
                    content = store.get(f"{round_number:03d}.bin")
                except FileNotFoundError:
                    continue
                if content != payload(round_number, size):
                    partial += 1
                seen.add(round_number)
        results.append({"seen": len(seen), "partial": partial, "torn_checkpoint": torn_checkpoint})
    else:
        raise SystemExit("unknown mode")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
