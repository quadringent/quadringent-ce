"""Catalog of laboratory scenarios (the 20-row matrix plus a synthetic baseline).

Each scenario is data: the boundary conditions to inject, optional prepare
steps and transport faults, and the expected behavior used by the matrix in
``docs/product/local-lab.md``. The automated assertions live in
``tests/test_lab_scenarios.py`` (backend) and
``ui/scripts/lab-browser-scenarios.mjs`` (browser).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import fixtures

PREPARE_STEPS = ("source_authenticated", "destination_verified")


@dataclass(frozen=True)
class Scenario:
    number: int
    name: str
    title: str
    injected: str
    backend: str
    visible: str
    allowed: str
    coverage: str  # backend | backend+browser | browser | cli
    conditions: dict[str, object] = field(default_factory=dict)
    prepare: tuple[str, ...] = ()
    faults: tuple[dict[str, object], ...] = ()
    live_injected: bool = False

    @property
    def backend_test(self) -> str:
        return f"test_scenario_{self.number:02d}_{self.name}"

    @property
    def browser_check(self) -> str:
        return f"scenario {self.number:02d} {self.name}"

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "name": self.name,
            "title": self.title,
            "injected": self.injected,
            "backend": self.backend,
            "visible": self.visible,
            "allowed": self.allowed,
            "coverage": self.coverage,
            "conditions": dict(self.conditions),
            "prepare": list(self.prepare),
            "faults": [dict(fault) for fault in self.faults],
            "live_injected": self.live_injected,
            "backend_test": self.backend_test if "backend" in self.coverage else None,
            "browser_check": self.browser_check if "browser" in self.coverage else None,
        }


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        number=0,
        name="baseline",
        title="Synthetic baseline (not part of the matrix)",
        injected="Default start state: simulation evidence, nothing verified.",
        backend="Evidence kind is simulation; no stage is ever reported as live.",
        visible="Cockpit labels every figure as synthetic.",
        allowed="All operator actions.",
        coverage="backend",
    ),
    Scenario(
        number=1,
        name="environment_ready",
        title="Environment ready",
        injected="Probe ok, destination verifier ok, tables ready, evidence injected as live (laboratory condition).",
        backend="Diagnostics overall status ready; every required result passed.",
        visible="Diagnostics screen shows ready; evidence labelled live.",
        allowed="All operator actions.",
        coverage="backend+browser",
        conditions={"evidence": "live"},
        prepare=("source_authenticated", "destination_verified"),
        live_injected=True,
    ),
    Scenario(
        number=2,
        name="tool_missing",
        title="Required local tool absent",
        injected="A tool such as java or kubectl is not installed on the operator machine.",
        backend="Not a server condition: the control plane never inspects local tools.",
        visible="Reported by the local `quadringent doctor` report.",
        allowed="Fix the machine, then rerun the doctor.",
        coverage="cli",
    ),
    Scenario(
        number=3,
        name="tcp_unreachable",
        title="IBM i TCP unreachable",
        injected="Source probe reports the network step failed.",
        backend="POST sources/test returns reachable=false, network failed, TLS and authentication not attempted.",
        visible="Network step failed; source authentication stays unknown.",
        allowed="Test again after fixing the route; table discovery is not offered as proven.",
        coverage="backend",
        conditions={"probe": "network_down"},
    ),
    Scenario(
        number=4,
        name="tls_invalid",
        title="TCP ok but TLS invalid",
        injected="Source probe reports an untrusted certificate chain.",
        backend="reachable=false, tls.ok=false, trust=unknown with the measured fingerprint; nothing is persisted.",
        visible="TLS step failed with a fingerprint to compare; source TLS stays unknown.",
        allowed="Pin the certificate explicitly; never trusted silently.",
        coverage="backend",
        conditions={"probe": "tls_invalid"},
    ),
    Scenario(
        number=5,
        name="auth_denied",
        title="Authentication refused",
        injected="Source probe reports TLS ok and authentication refused.",
        backend="reachable=false, authentication.ok=false; version and fingerprint are not persisted.",
        visible="Authentication step failed; source authentication stays unknown.",
        allowed="Correct the credentials and test again.",
        coverage="backend",
        conditions={"probe": "auth_denied"},
    ),
    Scenario(
        number=6,
        name="journal_absent",
        title="Journal absent",
        injected="Discovery returns a table that is not journaled.",
        backend="Table readiness not_journaled with CL commands to fix it.",
        visible="Table shown as not ready with its fix commands.",
        allowed="Read-only; the service does not gate the start of a pipeline (see limits).",
        coverage="backend",
        conditions={"discovery": "journal_absent"},
    ),
    Scenario(
        number=7,
        name="journal_config_incompatible",
        title="Incompatible journal configuration",
        injected="Discovery returns a journaled table with after-images only.",
        backend="Table readiness images_incomplete with the CHGJRNOBJ fix command.",
        visible="Table shown as not ready: before images missing.",
        allowed="Read-only; the service does not gate the start of a pipeline (see limits).",
        coverage="backend",
        conditions={"discovery": "journal_config"},
    ),
    Scenario(
        number=8,
        name="no_usable_key",
        title="Table without a usable key",
        injected="Discovery returns a journaled table with no primary key or unique index.",
        backend="Readiness no_key; choosing RRN without acknowledgement is refused; with acknowledgement it becomes ready.",
        visible="Table needs a key decision before it is ready.",
        allowed="Choose a key or explicitly acknowledge RRN identification.",
        coverage="backend",
        conditions={"discovery": "no_key"},
    ),
    Scenario(
        number=9,
        name="destination_privileges",
        title="Destination privileges insufficient",
        injected="Destination verifier reports the role cannot create tables.",
        backend="Verify returns verified=false with the failing step; state becomes failed.",
        visible="Destination shown as failed with the step that failed.",
        allowed="Fix grants and verify again; the service does not gate the start of a pipeline (see limits).",
        coverage="backend",
        conditions={"verifier": "insufficient_privileges"},
    ),
    Scenario(
        number=10,
        name="control_plane_down",
        title="Control plane unavailable",
        injected="The lab process is stopped: the port is closed.",
        backend="No answer: connection refused.",
        visible="Cockpit shows `Service unavailable`.",
        allowed="Refresh retries; no action is offered as done.",
        coverage="backend+browser",
    ),
    Scenario(
        number=11,
        name="read_without_delivery",
        title="Reading available but delivery not established",
        injected="Evidence: reading observed (injected live), delivery and reconciliation absent.",
        backend="Capture reading passed; delivery and reconciliation unknown; overall incomplete.",
        visible="Delivery and reconciliation shown as not measured.",
        allowed="All operator actions; no claim of end-to-end delivery.",
        coverage="backend+browser",
        conditions={"evidence": "reading_only"},
        live_injected=True,
    ),
    Scenario(
        number=12,
        name="evidence_absent",
        title="Evidence absent",
        injected="The observation provider returns no evidence.",
        backend="Evidence kind absent; every stage absent; diagnostics unknown, never passed.",
        visible="`No evidence collected`; nothing inferred from the declared state.",
        allowed="All operator actions.",
        coverage="backend+browser",
        conditions={"evidence": "absent"},
    ),
    Scenario(
        number=13,
        name="evidence_stale",
        title="Evidence stale",
        injected="Evidence: delivery and reconciliation observed three hours ago (injected live).",
        backend="Delivery stage stale; diagnostics delivery failed with a since timestamp; overall blocked.",
        visible="Delivery shown as stale with its observation date.",
        allowed="All operator actions; refresh does not hide staleness.",
        coverage="backend+browser",
        conditions={"evidence": "stale"},
        live_injected=True,
    ),
    Scenario(
        number=14,
        name="late_old_pipeline_response",
        title="Late response of a former pipeline",
        injected="The evidence read of pipeline A is delayed 3 s at the transport level.",
        backend="Both responses complete; each payload names its own pipeline.",
        visible="After navigating to pipeline B, the late answer of A never replaces B on screen.",
        allowed="Navigation stays possible while a read is pending.",
        coverage="backend+browser",
        faults=(
            {
                "id": "late-orders-evidence",
                "mode": "delay_before",
                "method": "GET",
                "path": f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/evidence",
                "seconds": 3.0,
            },
        ),
    ),
    Scenario(
        number=15,
        name="action_confirmed_reread",
        title="Action confirmed, effect read back",
        injected="Executor accepts; a sensitive action goes through the confirmation flow.",
        backend="409 pending confirmation, approve, execute, then GET returns the new state; executor ran once.",
        visible="Pending confirmation listed; after execution the new state is read back.",
        allowed="Approve or reject the pending confirmation; no unconfirmed sensitive action.",
        coverage="backend+browser",
    ),
    Scenario(
        number=16,
        name="timeout_effect_unknown",
        title="Timeout with effect still unknown",
        injected="The pause request executes, but its response is withheld for 4 s (client times out).",
        backend="Client timeout; GET shows the effect applied; replay with the same key runs the executor once.",
        visible="Result reported as unknown until the state is read back; no second mutation.",
        allowed="Read back the state; replay only with the same idempotency key.",
        coverage="backend+browser",
        faults=(
            {
                "id": "lost-pause-response",
                "mode": "delay_after",
                "method": "POST",
                "path": f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/actions/pause",
                "seconds": 4.0,
                "remaining": 1,
            },
        ),
    ),
    Scenario(
        number=17,
        name="reader_without_rights",
        title="User without rights",
        injected="A reader signs in (read scope only).",
        backend="Reads allowed; actions, confirmations and audit refused with 403 insufficient_role.",
        visible="Read-only cockpit; mutations are refused by the service.",
        allowed="Read only.",
        coverage="backend",
    ),
    Scenario(
        number=18,
        name="cross_organization",
        title="Read across two organizations",
        injected="Two organizations share one engine; the other organization's user reads.",
        backend="Destinations, users, confirmations isolated; other organization's session refused. See limits for sources/pipelines.",
        visible="Nothing of the other organization is visible.",
        allowed="Read own organization only.",
        coverage="backend",
    ),
    Scenario(
        number=19,
        name="export_canaries",
        title="Export containing sensitive data to omit",
        injected="Seed carries fictional canaries in host, account, secrets, logs, evidence and table comment.",
        backend="Diagnostic export contains none of the canaries; the preview lists what was omitted or pseudonymized.",
        visible="Export preview shown before download.",
        allowed="Download only after the preview.",
        coverage="backend+browser",
        conditions={"canaries": True},
    ),
    Scenario(
        number=20,
        name="resume_after_refresh",
        title="Resume after browser refresh",
        injected="A pending confirmation exists; the browser is reloaded and the service restarted over the same file.",
        backend="Pending confirmation and pipeline state survive a runtime restart.",
        visible="After reload the same screen and the pending confirmation are shown again.",
        allowed="Continue the pending confirmation.",
        coverage="backend+browser",
    ),
)

BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}
BY_NUMBER = {scenario.number: scenario for scenario in SCENARIOS}
DEFAULT_SCENARIO = "baseline"


class UnknownScenarioError(KeyError):
    pass


def get_scenario(identifier: str | int) -> Scenario:
    try:
        if isinstance(identifier, int) or str(identifier).isdigit():
            return BY_NUMBER[int(identifier)]
        return BY_NAME[str(identifier)]
    except KeyError as error:
        raise UnknownScenarioError(str(identifier)) from error
