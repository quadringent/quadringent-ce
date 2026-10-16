"""Replay real archived collection bytes without trusting its supplied verdict."""
from copy import deepcopy
from dataclasses import replace
import json
import shutil

import pytest

from quadringent_control_plane.v2.services.native_artifacts import ArtifactArchive
from quadringent_control_plane.v2.services.native_replay import NativeReplayError, StoredCollectionReplay
from test_native_source_observation import offline_java  # noqa: F401
from test_v2_native_collection import (  # noqa: F401
    ROOT, artifact_bytes, case, collected_case, collector, preparer, authority,
)


@pytest.fixture(autouse=True)
def native_replay_runtime(collected_case, offline_java):
    collected_case.replay_config = offline_java


def collect(value):
    return collector(value).collect(value.prepared, context=value.case.context,
                                    directory=value.directory, check_current=lambda: True)


def replay(value, result, tmp_path, **changes):
    return StoredCollectionReplay(value.case.storage, ROOT, "org-one", **value.replay_config).replay(
        value.prepared, result.manifest, context=value.case.context, directory=tmp_path / "replay", **changes)


def new_manifest(value, result, change):
    document = json.loads(artifact_bytes(value.case, result.manifest))
    change(document)
    archive = ArtifactArchive(value.case.storage, "org-one")
    manifest = archive.retain_bytes("collection_manifest", (json.dumps(document) + "\n").encode())
    return replace(result, manifest=manifest)


def test_replay_reads_archives_after_workspace_and_live_inputs_are_removed(collected_case, tmp_path):
    value = collected_case
    result = collect(value)
    shutil.rmtree(value.directory)
    live = value.case.storage.object_store("")
    for path in live.root.rglob("*"):
        if path.is_file() and not path.relative_to(live.root).parts[0].startswith("native-"):
            path.unlink()
    replayed = replay(value, result, tmp_path)
    assert replayed.comparison == result.report
    assert replayed.state == "incomplete"
    assert replayed.native_admission == "not_evaluated"
    assert replayed.native_cdc_qualified is False
    assert {"platform", "stable_cut", "physical_inventory"} <= set(replayed.missing_authorities)
    assert replayed.streams_replayed == 7
    assert not (tmp_path / "replay").exists()


@pytest.mark.parametrize("change", ["swapped_streams", "foreign_uri", "extra_field", "wrong_context", "fake_claim"])
def test_rehashed_manifest_cannot_replace_roles_scope_or_claim_admission(collected_case, tmp_path, change):
    value = collected_case
    result = collect(value)

    def tamper(doc):
        if change == "swapped_streams":
            doc["streams"]["source"], doc["streams"]["mirror"] = doc["streams"]["mirror"], doc["streams"]["source"]
        elif change == "foreign_uri":
            doc["comparison"]["uri"] = doc["comparison"]["uri"].replace("org-one", "other-org") + "x"
        elif change == "extra_field":
            doc["stable_cut"] = True
        elif change == "wrong_context":
            doc["context_sha256"] = "a" * 64
        else:
            doc["native_admission"] = "verified"

    result = new_manifest(value, result, tamper)
    with pytest.raises(NativeReplayError, match="native_archive_replay_failed"):
        replay(value, result, tmp_path)
    assert not (tmp_path / "replay").exists()


def test_rehashed_lying_comparison_is_rejected_by_actual_stream_recalculation(collected_case, tmp_path):
    value = collected_case
    result = collect(value)
    lie = deepcopy(result.report)
    lie["datasets"]["source"]["row_count"] += 1
    comparison = ArtifactArchive(value.case.storage, "org-one").retain_bytes(
        "comparison", (json.dumps(lie) + "\n").encode())
    result = new_manifest(value, result, lambda d: d.update(comparison=comparison.to_dict()))
    with pytest.raises(NativeReplayError):
        replay(value, result, tmp_path)


def test_replay_checks_context_before_reading_any_archive(collected_case, tmp_path, monkeypatch):
    value = collected_case
    result = collect(value)
    def forbidden(*args, **kwargs):
        pytest.fail("superseded replay touched provider")
    monkeypatch.setattr(type(value.case.storage), "object_store", forbidden)
    with pytest.raises(NativeReplayError):
        replay(value, result, tmp_path, check_current=lambda: False)


def test_rehashed_inventory_cannot_invent_receipt_selection(collected_case, tmp_path):
    value = collected_case
    result = collect(value)
    doc = json.loads(artifact_bytes(value.case, result.manifest))
    inventory = json.loads(artifact_bytes(value.case, replace(result.manifest, **doc["raw_inventory"])))
    inventory["receipts"] = [{"selection": "selected", "event_count": 0}]
    reference = ArtifactArchive(value.case.storage, "org-one").retain_bytes(
        "raw_inventory", (json.dumps(inventory) + "\n").encode())
    result = new_manifest(value, result, lambda d: d.update(raw_inventory=reference.to_dict()))
    with pytest.raises(NativeReplayError):
        replay(value, result, tmp_path)


def test_supersession_cleanup_preserves_a_directory_that_replaced_the_owned_one(collected_case, tmp_path):
    value = collected_case
    result = collect(value)
    path = tmp_path / "replay"
    replaced = False

    def current():
        nonlocal replaced
        if not replaced and (path / "ledger").exists():
            path.rename(tmp_path / "old-replay")
            path.mkdir()
            (path / "unrelated").write_bytes(b"preserve unrelated work")
            replaced = True
        return not replaced

    with pytest.raises(NativeReplayError, match="^native_archive_replay_failed$"):
        replay(value, result, tmp_path, check_current=current)
    assert replaced and (path / "unrelated").read_bytes() == b"preserve unrelated work"


def test_consistently_rehashed_recorded_payload_must_match_the_object_actually_consumed(collected_case, tmp_path):
    value = collected_case
    result = collect(value)
    outer = json.loads(artifact_bytes(value.case, result.manifest))
    inner = json.loads(artifact_bytes(value.case, replace(result.manifest, **outer["collection"])))
    archive = ArtifactArchive(value.case.storage, "org-one")
    index = next(i for i, item in enumerate(outer["originals"])
                 if item["dataset"] is None and item["object_key"] == value.case.payload_key)
    original = outer["originals"][index]["artifact"]
    changed = archive.retain_bytes(original["name"], b"different recorded payload\n").to_dict()
    outer["originals"][index]["artifact"] = changed
    inner["originals"][index].update(sha256=changed["sha256"], byte_count=changed["byte_count"])
    previous_collection = outer["collection"]
    outer["collection"] = archive.retain_bytes("collection", (json.dumps(inner) + "\n").encode()).to_dict()
    for item in outer["files"]:
        if item["artifact"] == original:
            item["artifact"] = changed
        elif item["artifact"] == previous_collection:
            item["artifact"] = outer["collection"]
    result = replace(result, manifest=archive.retain_bytes("collection_manifest", (json.dumps(outer) + "\n").encode()))
    with pytest.raises(NativeReplayError):
        replay(value, result, tmp_path)


def test_rehashed_phase_only_guards_cannot_stand_in_for_original_native_observations(collected_case, tmp_path):
    value=collected_case
    result=collect(value)
    doc=json.loads(artifact_bytes(value.case,result.manifest))
    archive=ArtifactArchive(value.case.storage,'org-one')
    for name in ('journal_original','final_original','stdout_original'):
        previous=doc['guards'][name]
        raw=json.loads(artifact_bytes(value.case,replace(result.manifest,**previous)))
        raw.pop('current_observations',None)
        phases=['before_business','after_business'] if name=='journal_original' else ['after_all_streams']
        raw['guards']=[{'phase':phase} for phase in phases]
        retained=archive.retain_bytes('guard:'+name,(json.dumps(raw)+'\n').encode()).to_dict()
        doc['guards'][name]=retained
        for item in doc['files']:
            if item['artifact']==previous:item['artifact']=retained
    manifest=archive.retain_bytes('collection_manifest',(json.dumps(doc)+'\n').encode())
    with pytest.raises(NativeReplayError,match='native_archive_replay_failed'):
        replay(value,replace(result,manifest=manifest),tmp_path)
