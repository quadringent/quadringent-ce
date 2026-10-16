"""Build metadata is baked from the selected source, never supplied over HTTP."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from quadringent_control_plane.v2.build_identity import (
    BuildIdentity, read_build_identity, write_build_identity,
)


REVISION = "abcde" * 8


def project(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nversion = "0.4.5"\n')
    return path


def test_build_identity_round_trip_preserves_source_and_package_version(tmp_path):
    target = tmp_path / "_build_identity.json"
    write_build_identity(REVISION, project(tmp_path), target)
    assert read_build_identity(target) == BuildIdentity(REVISION, "0.4.5")
    assert json.loads(target.read_bytes()) == {
        "format_version": "quadringent-build-identity-v1",
        "source_revision": REVISION, "version": "0.4.5",
    }
    assert target.stat().st_mode & 0o222 == 0


def test_unstamped_development_build_does_not_invent_a_revision(tmp_path):
    target = tmp_path / "_build_identity.json"
    write_build_identity("", project(tmp_path), target)
    assert not target.exists()
    assert read_build_identity(target) is None


@pytest.mark.parametrize("revision", ["main", "A" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n"])
def test_invalid_build_revision_does_not_create_metadata(tmp_path, revision):
    target = tmp_path / "_build_identity.json"
    with pytest.raises(ValueError):
        write_build_identity(revision, project(tmp_path), target)
    assert not target.exists()


@pytest.mark.parametrize("revision", [REVISION, ""])
def test_build_never_reuses_or_overwrites_preexisting_metadata(tmp_path, revision):
    target = tmp_path / "_build_identity.json"
    target.write_bytes(b"unrelated")
    with pytest.raises((ValueError, FileExistsError)):
        write_build_identity(revision, project(tmp_path), target)
    assert target.read_bytes() == b"unrelated"


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.replace(b'"version":"0.4.5"', b'"version":"0.4.5","version":"0.4.6"'),
    lambda raw: raw[:-1] + b',"extra":true}',
    lambda raw: raw.replace(REVISION.encode(), b"main"),
    lambda raw: raw.replace(b'"0.4.5"', b"NaN"),
    lambda raw: raw + b"{}",
    lambda raw: b" " * 4097,
])
def test_invalid_baked_document_is_rejected(tmp_path, mutation):
    target = tmp_path / "_build_identity.json"
    raw = json.dumps({"format_version": "quadringent-build-identity-v1",
                      "source_revision": REVISION, "version": "0.4.5"}, separators=(",", ":")).encode()
    target.write_bytes(mutation(raw))
    with pytest.raises(ValueError):
        read_build_identity(target)


def test_metadata_symlink_is_not_followed(tmp_path):
    target = tmp_path / "_build_identity.json"
    original = tmp_path / "real.json"
    write_build_identity(REVISION, project(tmp_path), original)
    target.symlink_to(original)
    with pytest.raises(ValueError):
        read_build_identity(target)


def test_short_read_cannot_hide_a_corrupted_identity_suffix(tmp_path, monkeypatch):
    target = tmp_path / "_build_identity.json"
    prefix = json.dumps({"format_version": "quadringent-build-identity-v1",
                         "source_revision": REVISION, "version": "0.4.5"}).encode()
    target.write_bytes(prefix + b"{}")
    monkeypatch.setattr(os, "read", lambda descriptor, maximum: prefix)
    with pytest.raises(ValueError):
        read_build_identity(target)


def test_local_metadata_is_excluded_from_the_image_context():
    import pathspec

    root = Path(__file__).resolve().parents[1]
    spec = pathspec.PathSpec.from_lines("gitignore", (
        root / "docker/control-plane.Dockerfile.dockerignore").read_text().splitlines())
    assert spec.match_file("src/quadringent_control_plane/v2/_build_identity.json")


def test_image_build_command_generates_identity_inside_the_packaged_tree(tmp_path):
    from test_docker_build_context import _runtime_python_tree

    root = Path(__file__).resolve().parents[1]
    src = _runtime_python_tree(root / "docker/control-plane.Dockerfile",
                               root / "docker/control-plane.Dockerfile.dockerignore", tmp_path / "image")
    package = src / "quadringent_control_plane"
    module = package / "v2"
    command = (f"import sys,runpy;sys.path.insert(0,{str(package.parent)!r});"
               f"sys.argv=['build_identity','--revision',{REVISION!r},'--pyproject',{str(project(tmp_path))!r}];"
               "runpy.run_module('quadringent_control_plane.v2.build_identity',run_name='__main__')")
    result = subprocess.run([sys.executable, "-I", "-c", command], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert read_build_identity(module / "_build_identity.json") == BuildIdentity(REVISION, "0.4.5")


def test_image_verifier_stdin_uses_packaged_import_root_beside_the_launcher(tmp_path):
    from test_docker_build_context import _runtime_python_tree

    root = Path(__file__).resolve().parents[1]
    src = _runtime_python_tree(root / "docker/control-plane.Dockerfile",
                               root / "docker/control-plane.Dockerfile.dockerignore", tmp_path / "image")
    app = src.parent
    (app / "quadringent_control_plane.py").write_bytes((root / "scripts/quadringent_control_plane.py").read_bytes())
    write_build_identity(REVISION, project(tmp_path), src / "quadringent_control_plane/v2/_build_identity.json")
    script = (root / "scripts/test_control_plane_image.sh").read_text()
    invocation = next(line for line in script.splitlines() if line.startswith("docker exec -i -e "))
    flags = shlex.split(invocation.split(" python ", 1)[1].split(" <<", 1)[0])
    code = script.split("<<'PY'\n", 1)[1].split("\nfor attempt in range(100):", 1)[0]
    code += ("\nimport sys\n"
             "for name,module in list(sys.modules.items()):\n"
             "    if name.startswith(('quadringent.', 'quadringent_control_plane')):\n"
             f"        assert Path(module.__file__).resolve().is_relative_to(Path({str(src)!r})), name\n")
    result = subprocess.run([sys.executable, *flags], input=code, cwd=app, capture_output=True,
                            text=True, timeout=15, env={"PATH": os.environ["PATH"],
                            "PYTHONPATH": str(src), "EXPECTED_BUILD_REVISION": REVISION})
    assert result.returncode == 0, result.stderr
