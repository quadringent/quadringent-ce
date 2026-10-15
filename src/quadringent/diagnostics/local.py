"""Local tool and configuration checks; credential values are never read out."""

from __future__ import annotations

from typing import Mapping

from .model import DiagnosticResult, Subject, utc_now

AWS_CREDENTIAL_ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_ROLE_ARN")
GCP_CREDENTIAL_ENV_VARS = ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CORE_ACCOUNT")


def tool_results(which, tools: tuple[str, ...], *, required: bool = True) -> list[DiagnosticResult]:
    results = []
    for tool in tools:
        found = which(tool) is not None
        results.append(DiagnosticResult(
            code=f"local_tools.{tool.replace('-', '_')}", layer="local_tools",
            status="passed" if found else "failed", required=required, observed_at=utc_now(),
            provenance="local:PATH", subject=Subject("tool", tool),
            summary=f"{tool} is {'available' if found else 'not found'} on PATH.",
            next_action="No action." if found else f"Install {tool} and make sure it is on PATH, then run the doctor again.",
            limits="Presence on PATH only; the tool version and its credentials are not checked.",
            evidence={"tool": tool, "found": found},
        ))
    return results


def cloud_credentials_result(cloud: str, environ: Mapping[str, str]) -> DiagnosticResult:
    names = AWS_CREDENTIAL_ENV_VARS if cloud == "aws" else GCP_CREDENTIAL_ENV_VARS
    present = [name for name in names if environ.get(name)]
    return DiagnosticResult(
        code="local_config.cloud_credentials", layer="local_config", status="passed" if present else "failed",
        required=True, observed_at=utc_now(), provenance="local:environment", subject=Subject("tool", cloud),
        summary=(f"{cloud.upper()} credential configuration detected ({', '.join(present)})." if present
                 else f"No {cloud.upper()} credential configuration detected."),
        next_action=("No action." if present
                     else f"Configure one of {', '.join(names)} for the identity that will run the installer."),
        limits="Presence of configuration only, not authenticated: no cloud API is called and values are never shown.",
    )
