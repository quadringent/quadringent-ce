"""Resolve pinned preparation originals without consulting live checkpoints."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from urllib.parse import urlsplit

import pytest

from quadringent.storage_backend import StorageBackend
from quadringent_control_plane.v2.services.native_resolution import (
    NativeResolutionError, PreparationResolver, ResolutionLimits,
)
from quadringent_control_plane.v2.services.native_verifications import NativeArtifact
from test_v2_native_preparation import ROOT, case, load_reference, manifest, preparer  # noqa: F401
from test_v2_native_verifications import authority  # noqa: F401


def resolver(case, **kwargs):
    return PreparationResolver(case.storage, ROOT, "org-one", **kwargs)


def rewrite(case, prepared, change):
    document = manifest(case, prepared)
    change(document)
    content = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    old = next(a for a in prepared.artifacts if a.name == "preparation_manifest")
    digest = hashlib.sha256(content).hexdigest()
    ref = NativeArtifact(old.name, old.uri[:-64] + digest, digest, "sha256:" + digest, len(content))
    case.store.put_once(urlsplit(ref.uri).path.lstrip("/"), content)
    return replace(prepared, artifacts=tuple(ref if a.name == ref.name else a for a in prepared.artifacts))


def test_real_preparation_resolves_archives_after_live_originals_are_removed(case, monkeypatch):
    prepared = preparer(case).prepare(case.context)
    document = manifest(case, prepared)
    for item in document["originals"]:
        (case.store.root / item["source_key"]).unlink()
    monkeypatch.setattr(StorageBackend, "checkpoint_store", lambda *_: pytest.fail("live C read"))
    result = resolver(case).resolve(prepared, context=case.context)
    assert result.scope == prepared.scope
    assert result.context_sha256 == document["context_sha256"]
    assert result.source_id == "source-one" and result.destination_id == "destination-one"
    assert set(result.store.references) == {item["source_key"] for item in document["originals"]}
    assert result.store.get_bounded(result.initial_copy.key, 65536) == load_reference(
        case, next(a for a in prepared.artifacts if a.name == "copy_evidence")
    )
    result.verify()
    with pytest.raises(NativeResolutionError):
        result.store.get_bounded("unknown/live/key", 65536)
    with pytest.raises(TypeError):
        result.store.references["injected"] = prepared.artifacts[0]


def test_resolution_rejects_changed_sql_context_and_scope(case):
    prepared = preparer(case).prepare(case.context)
    context = deepcopy(case.context)
    context["generation"] += 1
    with pytest.raises(NativeResolutionError):
        resolver(case).resolve(prepared, context=context)
    scope = replace(prepared.scope, mirror_table="OTHER_MIRROR")
    with pytest.raises(NativeResolutionError):
        resolver(case).resolve(replace(prepared, scope=scope), context=case.context)


@pytest.mark.parametrize("change", [
    lambda d: d.update(unexpected=True),
    lambda d: d.update(native_cdc_qualified=True),
    lambda d: d.update(native_cdc_qualified=0),
    lambda d: d.update(org_id="another-org"),
    lambda d: d.update(source_id="another-source"),
    lambda d: d["checkpoint_observation"].update(stream_key="another-stream"),
    lambda d: d["checkpoint_observation"].update(encoding="dynamodb-attribute-item-v1"),
    lambda d: d["registry_inventory"].update(coverage="complete"),
    lambda d: d["originals"].append(deepcopy(d["originals"][0])),
    lambda d: d["originals"].pop(),
    lambda d: d["originals"][0].update(source_uri="local://other-bucket/key"),
    lambda d: d["originals"][0].update(archived_uri="local://synthetic-proof/outside/" + "a" * 64),
    lambda d: d["artifacts"].pop(),
])
def test_resolver_refuses_semantically_invalid_content_addressed_manifest(case, change):
    prepared = preparer(case).prepare(case.context)
    malformed = rewrite(case, prepared, change)
    with pytest.raises(NativeResolutionError):
        resolver(case).resolve(malformed, context=case.context)


def test_resolution_rechecks_original_bytes_and_never_falls_back_to_live(case):
    prepared = preparer(case).prepare(case.context)
    result = resolver(case).resolve(prepared, context=case.context)
    ref = result.store.references[case.payload_key]
    (case.store.root / urlsplit(ref.uri).path.lstrip("/")).write_bytes(b"corrupted")
    assert case.store.get_bounded(case.payload_key, 65536)
    with pytest.raises(NativeResolutionError):
        result.store.get_bounded(case.payload_key, 65536)
    with pytest.raises(NativeResolutionError):
        result.verify()


def test_resolution_is_lazy_bounded_and_requires_strict_current(case, monkeypatch):
    prepared = preparer(case).prepare(case.context)
    with monkeypatch.context() as patch:
        patch.setattr(StorageBackend, "object_store", lambda *_: pytest.fail("unexpected IO"))
        configured = resolver(case)
        for value in (False, 1, None):
            with pytest.raises(NativeResolutionError):
                configured.resolve(prepared, context=case.context, check_current=lambda: value)
    for limits in (ResolutionLimits(max_object_bytes=1), ResolutionLimits(max_total_bytes=100),
                   ResolutionLimits(max_objects=1)):
        with pytest.raises(NativeResolutionError):
            resolver(case, limits=limits).resolve(prepared, context=case.context)


@pytest.mark.parametrize("kwargs", [{"max_objects": True}, {"max_seconds": 0}, {"max_rows": 20_000_001}])
def test_resolution_limits_are_strict(kwargs):
    with pytest.raises(ValueError):
        ResolutionLimits(**kwargs)


@pytest.mark.parametrize("case", ["aws", "gcs"], indirect=True)
def test_resolution_uses_stored_original_checkpoint_encoding_on_all_backends(case, monkeypatch):
    prepared = preparer(case).prepare(case.context)
    monkeypatch.setattr(StorageBackend, "checkpoint_store", lambda *_: pytest.fail("live C read"))
    result = resolver(case).resolve(prepared, context=case.context)
    assert result.backend == case.storage.kind
    result.verify()
