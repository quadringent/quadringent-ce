"Require all seven successful canonical CI jobs for the exact release commit."
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime

REQUIRED_JOBS = frozenset({"tests", "java", "chart", "image-runtime",
                           "verifier-runtime", "cockpit-runtime", "quadringent-product-gate"})


def validate_run(run: dict, repository: str, revision: str) -> None:
    if (run.get("repository", {}).get("full_name") != repository
            or run.get("head_repository", {}).get("full_name") != repository
            or run.get("head_sha") != revision
            or run.get("path", "").split("@", 1)[0] != ".github/workflows/ci.yml"
            or run.get("event") != "push" or run.get("head_branch") != "main"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or type(run.get("id")) is not int or run["id"] <= 0
            or type(run.get("run_attempt")) is not int or run["run_attempt"] <= 0):
        raise ValueError("successful canonical CI for the exact commit absent")


def validate_jobs(run: dict, pages: list[dict]) -> None:
    if not pages or any(not isinstance(page.get("jobs"), list) for page in pages):
        raise ValueError("CI check evidence absent")
    jobs = [job for page in pages for job in page["jobs"]]
    if (any(page.get("total_count") != len(jobs) for page in pages)
            or len(jobs) != len(REQUIRED_JOBS)
            or {job.get("name") for job in jobs} != REQUIRED_JOBS
            or len({job.get("id") for job in jobs}) != len(jobs)
            or any(job.get("run_id") != run["id"] or job.get("head_sha") != run["head_sha"]
                   or job.get("status") != "completed" or job.get("conclusion") != "success"
                   for job in jobs)):
        raise ValueError("all seven CI checks must pass")


def api(endpoint: str, *, paginated: bool = False):
    command = ["gh", "api", "-X", "GET", endpoint]
    if paginated:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("Cannot read GitHub CI evidence")
    return json.loads(result.stdout)


def check(repository: str, revision: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid GitHub repository")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("An exact commit SHA is required")
    prefix = f"repos/{repository}/actions"
    pages = api(f"{prefix}/workflows/ci.yml/runs?head_sha={revision}&branch=main"
                "&event=push&per_page=100", paginated=True)
    candidates = [run for page in pages for run in page["workflow_runs"]]
    if candidates:
        # Un ancien succès ne masque jamais un échec ou une CI encore active.
        run = max(candidates, key=lambda item: (datetime.fromisoformat(item["created_at"]), item["id"]))
        validate_run(run, repository, revision)
        attempt = run["run_attempt"]
        jobs = api(f"{prefix}/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100", paginated=True)
        validate_jobs(run, jobs)
        fresh = api(f"{prefix}/runs/{run['id']}")
        validate_run(fresh, repository, revision)
        if fresh["run_attempt"] != attempt:
            raise ValueError("CI attempt changed during verification")
        latest = api(f"{prefix}/workflows/ci.yml/runs?head_sha={revision}&branch=main"
                     "&event=push&per_page=100", paginated=True)
        observed = [candidate for page in latest for candidate in page["workflow_runs"]]
        if not observed:
            raise ValueError("CI evidence disappeared during verification")
        final = max(observed, key=lambda item: (datetime.fromisoformat(item["created_at"]), item["id"]))
        if final["id"] != run["id"]:
            raise ValueError("new CI started during verification")
        validate_run(final, repository, revision)
        if final["run_attempt"] != attempt:
            raise ValueError("CI attempt changed during verification")
        return {"repository": repository, "source_sha": revision,
                "ci_run_id": run["id"], "ci_run_attempt": attempt, "checks": sorted(REQUIRED_JOBS)}
    raise ValueError("no successful canonical CI for this commit; run CI before release")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.repository, args.sha), sort_keys=True))
    except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"CI validation refused: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
