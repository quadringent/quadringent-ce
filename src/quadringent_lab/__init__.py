"""Quadringent local laboratory: a deterministic, synthetic test harness.

The laboratory runs the real v2 control plane (``create_v2_app``, a throwaway
SQLite file) and serves the real built frontend (``ui/dist``) from the same
origin. IBM i, Snowflake, Kubernetes and the cloud are replaced only at the
injectable boundaries that the product already exposes (source probe, table
discovery, destination verifier, pipeline executor, observation provider,
log source, costs provider).

This package is a test tool. It is deliberately NOT part of the distributed
wheel (see ``[tool.setuptools.packages.find]`` in ``pyproject.toml``), and the
lab-only router is mounted by the laboratory gateway only, never by the
product application.

Everything the laboratory produces is synthetic. Records carry the provenance
``lab_fixture``; evidence is of kind ``simulation`` unless a scenario
explicitly injects ``live`` to exercise the green path (documented per
scenario in ``docs/product/local-lab.md``).
"""

from __future__ import annotations

LAB_MARKER_FILE = ".quadringent-lab"
LAB_PROVENANCE = "lab_fixture"
LAB_ORG_ID = "lab"
LAB_OTHER_ORG_ID = "lab-other"
DEFAULT_PORT = 8877
