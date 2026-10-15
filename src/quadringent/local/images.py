"""Image references of the two published containers.

The capture image carries the Java reader and the IBM i jobs; the control-plane
image carries the Snowflake connector. Both are tagged with the package version.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
import tomllib

from .config import IMAGE_REFERENCE, ConfigError

REGISTRY = "ghcr.io/quadringent/quadringent-ce"


def package_version_string() -> str:
    """Same result as ``quadringent_control_plane.version.version`` without importing that package.

    The host must not import the control plane (it pulls the runtime modules that need ``fcntl``).
    """
    try:
        return package_version("quadringent")
    except PackageNotFoundError:
        for parent in list(Path(__file__).resolve().parents)[:4]:
            source = parent / "pyproject.toml"
            if source.is_file():
                project = tomllib.loads(source.read_text())["project"]
                if project["name"] == "quadringent":
                    return str(project["version"])
        raise ConfigError("missing version metadata: install Quadringent") from None


def default_images(version: str | None = None) -> tuple[str, str]:
    tag = (version or package_version_string()).replace("+", "-")
    return f"{REGISTRY}:{tag}-capture", f"{REGISTRY}:{tag}-control-plane"


def resolve_images(capture: str | None, loader: str | None, *, version: str | None = None) -> tuple[str, str]:
    default_capture, default_loader = default_images(version)
    resolved = (capture or default_capture, loader or default_loader)
    for image in resolved:
        if not IMAGE_REFERENCE.fullmatch(image):
            raise ConfigError("invalid image reference: use name[:tag][@sha256:digest]")
    return resolved
