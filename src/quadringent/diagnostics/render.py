"""Human rendering of a diagnostic report; the JSON form is ``report.to_dict()``."""

from __future__ import annotations

from .model import LAYERS, DiagnosticReport

_MARK = {"passed": "PASS", "failed": "FAIL", "unknown": "UNKN", "skipped": "SKIP"}


def render_text(report: DiagnosticReport) -> str:
    overall = report.overall()
    lines = [
        f"Quadringent doctor: {str(overall['status']).upper()}  "
        f"({overall['failed']} failed, {overall['unknown']} unknown, {overall['skipped']} skipped, "
        f"{overall['passed']} passed)",
        f"Generated {report.generated_at} UTC; {overall['summary']}",
        "",
    ]
    order = {layer: index for index, layer in enumerate(LAYERS)}
    priority = {"failed": 0, "unknown": 1, "skipped": 2, "passed": 3}
    for item in sorted(report.results, key=lambda r: (priority[r.status], order[r.layer], r.code)):
        subject = f" [{item.subject.kind} {item.subject.label or item.subject.ref}]" if item.subject else ""
        lines.append(f"[{_MARK[item.status]}] {item.code}{subject}: {item.summary}")
        if item.status != "passed":
            lines.append(f"       Next: {item.next_action}")
        lines.append(f"       Limits: {item.limits} (observed {item.observed_at}, {item.provenance})")
    return "\n".join(lines) + "\n"
