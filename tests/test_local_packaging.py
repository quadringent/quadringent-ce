"""The local backend and ``local_runtime`` work from the files the published images carry.

The Dockerfiles copy fixed file lists, so a new top-level module under ``src/quadringent``
would be missing at run time. These tests stage each image's Python tree from its own
file list (as ``test_runtime_packaging`` does) and run the runtime there, with the
packages the image does not ship (SQLAlchemy, the Snowflake connector) made unimportable
in the capture image.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from local_support import LABEL, local_env, make_data_dir

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULE = "quadringent_control_plane.v2.executor.local_runtime"


def capture_image_modules() -> list[str]:
    """Files ``docker/Dockerfile`` copies into ``/app/quadringent`` (the image's own list, not the notes file)."""
    sources = []
    for line in (ROOT / "docker/Dockerfile").read_text().splitlines():
        fields = shlex.split(line) if line.startswith("COPY src/quadringent/") else []
        if fields and fields[-1] == "/app/quadringent/":
            sources.extend(fields[1:-1])
    return sources


def stage_capture_image(base: Path) -> Path:
    app = base / "app"
    (app / "quadringent").mkdir(parents=True)
    for line in (ROOT / "docker/Dockerfile").read_text().splitlines():
        fields = shlex.split(line) if line.startswith("COPY src/quadringent/") else []
        if fields and fields[-1].startswith("/app/quadringent/"):
            destination = app / fields[-1].removeprefix("/app/")
            destination.mkdir(parents=True, exist_ok=True)
            for source in fields[1:-1]:
                shutil.copy2(ROOT / source, destination / Path(source).name)
    shutil.copytree(ROOT / "src/quadringent_control_plane", app / "quadringent_control_plane",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for line in (ROOT / "docker/Dockerfile").read_text().splitlines():
        fields = shlex.split(line) if line.startswith("COPY scripts/") else []
        if fields and fields[-1] == "/app/":
            for source in fields[1:-1]:
                shutil.copy2(ROOT / source, app / Path(source).name)
    return app


def stage_control_plane_image(base: Path) -> Path:
    src = base / "app" / "src"
    (src / "quadringent").mkdir(parents=True)
    allowlist = set((ROOT / "docker/control-plane.Dockerfile.dockerignore").read_text().splitlines())
    for line in (ROOT / "docker/control-plane.Dockerfile").read_text().splitlines():
        fields = shlex.split(line) if line.startswith("COPY ") else []
        if fields and fields[-1] == "/app/src/quadringent/":
            for source in fields[1:-1]:
                if "!" + source in allowlist:
                    shutil.copy2(ROOT / source, src / "quadringent" / Path(source).name)
    shutil.copytree(ROOT / "src/quadringent_control_plane", src / "quadringent_control_plane",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return src


def run_isolated(path: Path, code: str, *, blocked=(), env=None):
    prelude = "import sys\nsys.path.insert(0, %r)\n" % str(path)
    prelude += "".join(f"sys.modules[{name!r}] = None\n" for name in blocked)
    environment = {"PATH": os.environ.get("PATH", ""), **(env or {})}
    return subprocess.run([sys.executable, "-I", "-c", prelude + code], capture_output=True, text=True,
                          timeout=60, env=environment)


def test_runtime_module_is_a_new_file_only_below_the_control_plane_package():
    tracked = subprocess.run(["git", "ls-files", "--others", "--cached", "--exclude-standard", "src/quadringent"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    top_level = {Path(path).name for path in tracked if Path(path).parent == Path("src/quadringent")}
    listed = {Path(path).name for path in capture_image_modules()}
    module_imports = {"checkpoint.py", "object_store.py", "source_gate.py", "storage_backend.py",
                      "capture_binding.py", "reader_scope.py"}
    assert module_imports <= listed, "the local backend lives in files the capture image already copies"
    assert (ROOT / "src/quadringent_control_plane/v2/executor/local_runtime.py").is_file()
    assert "local_runtime.py" not in top_level


def test_capture_image_runs_the_whole_local_runtime_without_sqlalchemy_or_snowflake(tmp_path):
    app = stage_capture_image(tmp_path)
    data = make_data_dir(tmp_path)
    blocked = ("sqlalchemy", "snowflake", "cryptography")
    imported = run_isolated(app, f"import {RUNTIME_MODULE}, quadringent.storage_backend; print('ok')", blocked=blocked)
    assert imported.returncode == 0 and imported.stdout.strip() == "ok", imported.stderr
    probe = run_isolated(
        app, f"import sys\nfrom {RUNTIME_MODULE} import main\nsys.exit(main(['fs-probe', '--data-dir', {str(data)!r}]))",
        blocked=blocked)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout.split("=", 1)[1])["ok"] is True
    # admission and the object, checkpoint and gate stores come from the same file list
    stores = run_isolated(app, """
import os, sys
from quadringent.storage_backend import StorageBackend
storage = StorageBackend.from_environment(dict(os.environ))
storage.object_store('p').put_once('k', b'v')
storage.checkpoint_store('s').load_bound()
storage.source_gate('g')
import quadringent_initial_copy_job, as400_continuous_capture, as400_snapshot_publish
print('stores-ok')
""", blocked=blocked, env=local_env(data))
    assert stores.returncode == 0 and stores.stdout.strip() == "stores-ok", stores.stderr


def test_control_plane_image_imports_the_runtime_and_the_snowflake_verifier_path(tmp_path):
    src = stage_control_plane_image(tmp_path)
    data = make_data_dir(tmp_path)
    code = (f"import {RUNTIME_MODULE} as runtime\n"
            "from quadringent.storage_backend import StorageBackend\n"
            "storage = StorageBackend.from_environment(__import__('os').environ.copy())\n"
            "print(storage.kind, runtime.DEFAULT_LOADER_SCRIPT)\n")
    result = run_isolated(src, code, env=local_env(data))
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["local", "/app/quadringent_destination_loader.py"]


@pytest.mark.parametrize("script", ["scripts/as400_continuous_capture.py", "scripts/quadringent_initial_copy_job.py",
                                    "scripts/as400_snapshot_publish.py"])
def test_capture_image_ships_the_scripts_with_local_branches(script):
    dockerfile = (ROOT / "docker/Dockerfile").read_text()
    assert f"COPY {script} /app/" in dockerfile
    assert '"local"' in (ROOT / script).read_text()


def test_loader_script_is_shipped_in_the_control_plane_image():
    assert "COPY scripts/quadringent_destination_loader.py /app/quadringent_destination_loader.py" in (
        ROOT / "docker/control-plane.Dockerfile").read_text()
