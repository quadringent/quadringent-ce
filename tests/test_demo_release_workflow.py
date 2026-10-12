"""Release demo assets must match the clean tag and their checksum."""
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest
import yaml

from verify_demo_release import verify_demo_release

VERSION = "0.3.5"
COMMIT = "a" * 40
NAME = f"quadringent-demo-{VERSION}"


def archive_fixture(tmp_path, *, change=None, omit=None, unsafe=None, owner=0):
    archive = tmp_path / f"{NAME}.tar.gz"
    manifest = {"product": "Quadringent Community", "kind": "synthetic-demo",
                "version": VERSION, "sourceCommit": COMMIT,
                "sourceDirty": False, "synthetic": True, "connections": "none"}
    manifest.update(change or {})
    files = {"PROVENANCE.json": json.dumps(manifest).encode(),
             **{name: b"fixture" for name in (
                 "LICENSE", "NOTICE", "third-party-notices.txt", "index.html",
                 "serve-demo.mjs", "README.txt")}}
    with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
        for name, payload in files.items():
            if name == omit:
                continue
            member = tarfile.TarInfo(f"{NAME}/{name}")
            member.size, member.uid = len(payload), owner
            tar.addfile(member, io.BytesIO(payload))
        if unsafe:
            member = tarfile.TarInfo(unsafe)
            payload = json.dumps({**manifest, "sourceCommit": "b" * 40, "sourceDirty": True}).encode()
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(str(archive) + ".sha256").write_text(f"{checksum}  {archive.name}\n")
    return archive


@pytest.mark.parametrize("change", [
    {"version": "different"}, {"sourceCommit": "b" * 40},
    {"sourceDirty": True}, {"synthetic": False}, {"connections": "live"},
    {"synthetic": 1}, {"sourceDirty": 0}, {"kind": "product"},
])
def test_valid_checksum_does_not_admit_wrong_archive_provenance(tmp_path, change):
    archive = archive_fixture(tmp_path, change=change)
    # A correct manifest outside the archive must not influence admission.
    (tmp_path / "PROVENANCE.json").write_text(json.dumps({"sourceCommit": COMMIT}))
    destination = tmp_path / "extracted"
    with pytest.raises(ValueError, match="clean release commit"):
        verify_demo_release(archive, destination, version=VERSION, source_commit=COMMIT)
    assert not destination.exists()


def test_extracts_the_verified_archive_with_required_licenses(tmp_path):
    archive = archive_fixture(tmp_path)
    root = verify_demo_release(archive, tmp_path / "extracted",
                               version=VERSION, source_commit=COMMIT)
    assert json.loads((root / "PROVENANCE.json").read_text())["sourceCommit"] == COMMIT
    assert (root / "LICENSE").read_bytes() == b"fixture"


def test_changed_archive_fails_before_extraction(tmp_path):
    archive = archive_fixture(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="checksum"):
        verify_demo_release(archive, tmp_path / "extracted",
                            version=VERSION, source_commit=COMMIT)


@pytest.mark.parametrize("omit", ["LICENSE", "NOTICE", "third-party-notices.txt"])
def test_missing_license_blocks_admission(tmp_path, omit):
    archive = archive_fixture(tmp_path, omit=omit)
    with pytest.raises(ValueError, match="missing"):
        verify_demo_release(archive, tmp_path / "extracted",
                            version=VERSION, source_commit=COMMIT)


@pytest.mark.parametrize("unsafe", ["../outside", f"{NAME}/../outside", f"{NAME}//LICENSE",
                                    f"{NAME}/PROVENANCE.json/"])
def test_unsafe_or_alias_paths_block_extraction(tmp_path, unsafe):
    archive = archive_fixture(tmp_path, unsafe=unsafe)
    with pytest.raises(ValueError, match="Unsafe"):
        verify_demo_release(archive, tmp_path / "extracted",
                            version=VERSION, source_commit=COMMIT)
    assert not (tmp_path / "outside").exists()


def test_private_owner_blocks_archive_admission(tmp_path):
    archive = archive_fixture(tmp_path, owner=501)
    with pytest.raises(ValueError, match="metadata"):
        verify_demo_release(archive, tmp_path / "extracted",
                            version=VERSION, source_commit=COMMIT)


def test_only_verified_and_scanned_archive_reaches_release_upload():
    jobs = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]
    steps = jobs["chart"]["steps"]
    names = ("Build synthetic demo from the release commit",
             "Verify synthetic demo provenance and checksum",
             "Scan extracted synthetic demo before upload")
    indices = [next(i for i, step in enumerate(steps) if step.get("name") == name)
               for name in names]
    upload = next(i for i, step in enumerate(steps)
                  if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert indices == sorted(indices) and indices[-1] < upload
    assert not any(steps[index].get("continue-on-error") for index in [*indices, upload])
    build, verify, scan = [steps[index]["run"] for index in indices]
    assert 'export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"' in build
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in verify
    assert 'test -z "$(git status --porcelain --untracked-files=no)"' in verify
    assert "python scripts/verify_demo_release.py" in verify
    assert "--config .gitleaks.toml --redact" in scan
    assert "demo-release-extracted/quadringent-demo-" in scan
    assert "dist/*.sha256" in steps[upload]["with"]["path"]
    release = next(step for step in jobs["release"]["steps"]
                   if step.get("uses", "").startswith("softprops/action-gh-release@"))
    assert "packages/**/*.sha256" in release["with"]["files"]
    assert "chart" in jobs["release"]["needs"]
