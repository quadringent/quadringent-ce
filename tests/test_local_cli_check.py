"""``quadringent local check``: quadringent.diagnostic.v1, no sign-on by default, doctor exit codes."""
from __future__ import annotations

import json

from local_cli_support import Cli, DockerStub, RESULT, probe_output, result
from quadringent.diagnostics.model import parse_report


class Recorder:
    def __init__(self, fail=()):
        self.ports, self.fail = [], set(fail)

    def __call__(self, address, timeout=None):
        self.ports.append(address[1])
        if address[1] in self.fail:
            raise ConnectionRefusedError()

        class Connection:
            def close(self):
                pass
        return Connection()


def handshake(context, host, port, timeout):
    return b"certificate-der"


def report(cli, *argv, connector=None, **kwargs):
    code = cli.invoke("check", "--json", *argv, connector=connector or Recorder(), handshake=handshake, **kwargs)
    return code, parse_report(json.loads(cli.out.getvalue()))


def by_code(parsed):
    return {item.code: item for item in parsed.results}


def test_default_check_never_signs_on_or_contacts_snowflake_and_is_incomplete(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    code, parsed = report(cli)
    assert code == 3 and parsed.overall()["status"] == "incomplete"
    results = by_code(parsed)
    assert results["authentication.ibmi_signon"].status == "unknown"
    assert results["source_prerequisites.table"].status == "skipped" and "--discover" in results["source_prerequisites.table"].next_action
    assert results["destination.snowflake"].status == "skipped" and "--snowflake" in results["destination.snowflake"].next_action
    assert cli.runner.runs() == []  # no container, hence no sign-on and no Snowflake connection
    assert parsed.software["distribution"] == "local-mode"


def test_tls_ports_and_the_server_mapper_are_probed_and_tls_is_verified(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    connector = Recorder()
    code, parsed = report(cli, connector=connector)
    assert connector.ports == [9471, 9476, 9475, 449]
    results = by_code(parsed)
    assert all(results[f"network.tcp.{label}"].status == "passed" for label in ("database", "signon", "command", "server_mapper"))
    assert all(results[f"tls.{label}"].status == "passed" for label in ("database", "signon", "command"))


def test_explicit_ports_skip_the_server_mapper(tmp_path):
    cli = Cli(tmp_path)
    cli.init("--database-port", "9500", "--signon-port", "9501", "--command-port", "9502")
    connector = Recorder()
    report(cli, connector=connector)
    assert connector.ports == [9500, 9501, 9502]


def test_plaintext_uses_the_plain_ports_warns_and_skips_tls(tmp_path):
    cli = Cli(tmp_path)
    cli.init("--allow-plaintext-ibmi", "--source-time-zone", "Europe/Paris")
    connector = Recorder()
    code, parsed = report(cli, connector=connector)
    assert connector.ports == [8471, 8476, 8475, 449]
    assert "unencrypted" in cli.err.getvalue()
    results = by_code(parsed)
    assert results["tls.plaintext_accepted"].status == "skipped" and not results["tls.plaintext_accepted"].required
    assert not any(code.startswith("tls.") and code != "tls.plaintext_accepted" for code in results)


def test_an_unreachable_port_blocks_with_exit_code_one(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    code, parsed = report(cli, connector=Recorder(fail={9476}))
    assert code == 1 and by_code(parsed)["network.tcp.signon"].status == "failed"


def test_all_flags_ready_when_every_remote_check_passes(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    code, parsed = report(cli, "--signon", "--discover", "--snowflake")
    results = by_code(parsed)
    assert code == 0, [(r.code, r.status) for r in parsed.results if r.status != "passed"]
    assert results["authentication.ibmi_signon"].status == "passed"
    assert results["source_prerequisites.probe"].status == "passed" and "IBM:QP0100CET" in results["source_prerequisites.probe"].summary
    assert results["source_prerequisites.table"].status == "passed"
    assert results["destination.snowflake"].status == "passed"
    assert cli.runner.roles() == ["probe", "discover", "verify"]


def test_signon_failure_is_reported_once_and_not_retried(tmp_path):
    runner = DockerStub()
    runner.probe = probe_output(auth=False, auth_detail="authentication rejected")
    cli = Cli(tmp_path, runner=runner)
    cli.init()
    code, parsed = report(cli, "--signon")
    results = by_code(parsed)
    assert code == 1 and results["authentication.ibmi_signon"].status == "failed"
    assert cli.runner.roles() == ["probe"]


def test_snowflake_check_names_the_failed_parts_without_secrets(tmp_path):
    bad = RESULT + json.dumps({"ok": False, "checks": {"connection": {"ok": True, "detail": "x"},
                                                       "load_privileges": {"ok": False, "detail": "no CREATE TABLE"}}}) + "\n"
    cli = Cli(tmp_path, runner=DockerStub(verify=result(("x",), bad, 1)))
    cli.init()
    code, parsed = report(cli, "--snowflake")
    item = by_code(parsed)["destination.snowflake"]
    assert code == 1 and item.status == "failed" and "load_privileges" in item.summary
    assert "snowflake-script" in item.next_action


def test_loose_permissions_block_the_check(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    cli.paths.snowflake_key.chmod(0o644)
    code, parsed = report(cli)
    assert code == 1 and by_code(parsed)["local_config.instance"].status == "failed"


def test_missing_docker_blocks_remote_checks_without_contacting_anything(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    cli.runner.available_tools = frozenset()
    code, parsed = report(cli, "--signon")
    results = by_code(parsed)
    assert code == 1 and results["local_tools.docker"].status == "failed" and "local_tools.docker_runtime" in results
    assert cli.runner.runs() == []


def test_text_output_is_the_doctor_rendering_with_the_local_title(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    assert cli.invoke("check", connector=Recorder(), handshake=handshake) == 3
    assert cli.out.getvalue().startswith("Quadringent local check: INCOMPLETE")


def test_a_refused_signon_stops_discovery_from_signing_on_again(tmp_path):
    runner = DockerStub()
    runner.probe = probe_output(auth=False, auth_detail="authentication rejected")
    cli = Cli(tmp_path, runner=runner)
    cli.init()
    code, parsed = report(cli, "--signon", "--discover", "--snowflake")
    results = by_code(parsed)
    assert code == 1 and results["authentication.ibmi_signon"].status == "failed"
    assert results["source_prerequisites.table"].status == "skipped"
    assert "refused" in results["source_prerequisites.table"].summary
    assert cli.runner.roles() == ["probe", "verify"]  # no second IBM i sign-on


def test_unsafe_instance_files_run_no_container(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    cli.paths.ibmi_password.chmod(0o644)
    code, parsed = report(cli, "--signon", "--discover", "--snowflake")
    results = by_code(parsed)
    assert code == 1 and results["local_config.instance"].status == "failed"
    assert cli.runner.runs() == []


def test_a_linked_secret_runs_no_container(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    other = tmp_path / "other-secret"
    other.write_text("x")
    other.chmod(0o600)
    cli.paths.ibmi_password.unlink()
    cli.paths.ibmi_password.symlink_to(other)
    code, parsed = report(cli, "--signon")
    assert code == 1 and by_code(parsed)["local_config.instance"].status == "failed"
    assert cli.runner.runs() == []


def test_network_next_actions_name_local_commands_only():
    from quadringent.diagnostics.model import DiagnosticResult, Subject, utc_now
    from quadringent.local.check import _local_next_action

    def probe(layer, status, next_action, evidence):
        return DiagnosticResult(code=f"{layer}.signon", layer=layer, status=status, required=True,
                                observed_at=utc_now(), provenance="probe:tls", subject=Subject("host", "h", "signon 9476/tls"),
                                summary="s", next_action=next_action, limits="l", evidence=evidence)

    tls = _local_next_action(probe("tls", "failed", "Provide ... with --ibmi-ca-file (and to the site as the CA Secret)",
                                   {"port": 9476, "error": "certificate not trusted"}))
    assert "quadringent local init" in tls.next_action and "Secret" not in tls.next_action
    skipped = _local_next_action(probe("tls", "unknown", "Run quadringent doctor --check-ibmi HOST --tls", {"port": 9476}))
    assert "quadringent doctor" not in skipped.next_action and "local check" in skipped.next_action
