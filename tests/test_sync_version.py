from pathlib import Path
import json

from sync_version import synchronize


def test_release_sync_updates_only_the_golden_version_metadata(tmp_path: Path) -> None:
    for directory in ("ui/src", "chart", "java", "tests/golden"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    for filename in ("ui/package.json", "ui/package-lock.json"):
        (tmp_path / filename).write_text(json.dumps({"version": "1.0.0"}))
    (tmp_path / "chart/Chart.yaml").write_text('version: 1.0.0\nappVersion: "1.0.0"\n')
    (tmp_path / "java/pom.xml").write_text('<project><version>1.0.0</version></project>')
    golden = tmp_path / "tests/golden/values-int-render.yaml"
    original = (
        '  app.kubernetes.io/version: "1.0.0"\n'
        '  - name: QUADRINGENT_VERSION\n    value: "1.0.0"\n'
        'image: example.invalid/capture:v1.0.0\n'
    )
    golden.write_text(original)

    pending = synchronize(tmp_path, check=True)

    assert "tests/golden/values-int-render.yaml" in pending
    assert golden.read_text() == original
    synchronize(tmp_path, check=False)
    assert golden.read_text() == (
        '  app.kubernetes.io/version: "1.2.3"\n'
        '  - name: QUADRINGENT_VERSION\n    value: "1.2.3"\n'
        'image: example.invalid/capture:v1.0.0\n'
    )
    assert synchronize(tmp_path, check=True) == []
