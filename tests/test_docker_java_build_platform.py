"""Keep JVM compilation native to the builder and runtime images on the target."""
from pathlib import Path
import shlex

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = ("Dockerfile", "control-plane.Dockerfile")


@pytest.mark.parametrize("name", DOCKERFILES)
def test_only_maven_uses_the_builder_platform(name):
    stages = [shlex.split(line) for line in (ROOT / "docker" / name).read_text().splitlines()
              if line.startswith("FROM ")]
    maven = [stage for stage in stages if any(token.startswith("maven:") for token in stage)]
    assert len(maven) == 1
    assert maven[0][1] == "--platform=$BUILDPLATFORM"
    assert maven[0][-2:] == ["AS", "build"]
    for stage in stages:
        if stage is not maven[0]:
            assert not any(token.startswith("--platform=") for token in stage)
    assert stages[-2][1].startswith("eclipse-temurin:21-jre-")
    assert stages[-2][-2:] == ["AS", "jre"]
    assert stages[-1][1].startswith("python:3.14-")


@pytest.mark.parametrize("name", DOCKERFILES)
def test_maven_exports_only_java_archives_to_target_runtime(name):
    lines = (ROOT / "docker" / name).read_text().splitlines()
    exported = [shlex.split(line)[2:] for line in lines if line.startswith("COPY --from=build ")]
    assert exported == [
        ["/workspace/java/target/as400-journal-reader.jar", "/app/probe.jar"],
        ["/workspace/java/target/dependency", "/app/lib"],
        ["/workspace/java/target/dependency-sources", "/usr/share/quadringent/sources"],
    ]
    assert "COPY --from=jre /opt/java/openjdk /opt/java/openjdk" in lines
