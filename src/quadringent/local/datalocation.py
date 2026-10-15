"""Where the data directory of a local instance lives: a host folder or a Docker named volume.

Docker Desktop (macOS, Windows, and the Linux edition) shares host folders with
the containers through a file-sharing layer whose hard links and locks do not
behave like a local file system between two containers. A Docker *named
volume* lives inside Docker's own Linux VM, on its own ext4 disk, so it is the
default there. On a plain Linux host the bind-mounted folder stays the default.

Nothing here decides whether a location is *safe*: the file system gate
(``fs-probe``, run by ``start``) does, for both locations.
"""

from __future__ import annotations

import sys

from ..installer.runner import CommandRunner
from .config import DATA_BIND, DATA_VOLUME

DOCKER_DESKTOP = "Docker Desktop"


def docker_info(runner: CommandRunner) -> tuple[bool, bool]:
    """``(answered, desktop)``: whether ``docker info`` answered, and whether it reports Docker Desktop."""
    if runner.which("docker") is None:
        return False, False
    result = runner.run(("docker", "info", "--format", "{{.OperatingSystem}}"))
    return bool(result.ok), bool(result.ok and DOCKER_DESKTOP in (result.stdout or ""))


def docker_desktop_reported(runner: CommandRunner) -> bool:
    """True when ``docker info`` reports Docker Desktop as the operating system of the engine."""
    return docker_info(runner)[1]


def default_data_location(runner: CommandRunner, platform: str | None = None) -> tuple[str, str]:
    """``(location, reason)`` chosen when ``--data-location`` is not given.

    Native Windows is not a host platform of the local package (it needs ``fcntl`` and POSIX user ids); on
    Windows the CLI runs inside WSL2, where ``sys.platform`` is ``linux`` and only ``docker info`` can tell
    that the engine is Docker Desktop.
    """
    platform = sys.platform if platform is None else platform
    answered, desktop = docker_info(runner)
    if desktop:
        return DATA_VOLUME, "Docker Desktop reported by docker info"
    if platform == "darwin":
        return DATA_VOLUME, "macOS host"
    if not answered:
        return DATA_BIND, ("Linux host; docker info did not answer, so Docker Desktop could not be detected: "
                           "the file system check of start fails on a Docker Desktop shared folder, and the "
                           "location cannot be changed afterwards (create the instance again with "
                           "--data-location volume)")
    return DATA_BIND, "Linux host"
