"""Shared helpers for the local-mode tests (no network, no containers)."""
from __future__ import annotations

import json
import os
from pathlib import Path

LABEL = "qdt-0a1b2c3d"
NO_MOUNTINFO = "/nonexistent/mountinfo"  # keeps the probe independent of the runner's tmp filesystem


def make_data_dir(root: Path, label: str = LABEL) -> Path:
    data = Path(root) / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    (data / ".quadringent-local").write_text(json.dumps({"format_version": 1, "label": label}))
    return data


def local_env(data: Path, label: str = LABEL, **extra: str) -> dict[str, str]:
    env = {"QUADRINGENT_STORAGE_BACKEND": "local", "AS400_RAW_BUCKET": label, "AS400_LOCAL_DATA_DIR": str(data)}
    env.update(extra)
    return env


def worker_env(data: Path, label: str = LABEL) -> dict[str, str]:
    src = Path(__file__).resolve().parents[1] / "src"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AS400_", "QUADRINGENT_"))}
    env.update(local_env(data, label))
    env["PYTHONPATH"] = str(src)
    return env
