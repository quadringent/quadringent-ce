"""Discovery output drives readiness; not-ready tables print CL commands and nothing is changed on the IBM i."""
from __future__ import annotations

import json

import pytest

from local_cli_support import Cli, DockerStub, discover_output, table_row
from quadringent.local import discovery as disc
from quadringent.table_discovery import READY, IMAGES_INCOMPLETE, NOT_JOURNALED, NO_KEY


def assess(**kwargs):
    rows = disc.parse_discovery(discover_output(table_row(**kwargs)))
    return disc.assess_table(disc.find_table(rows, "QDCLIB", "ORDERS"))


def test_a_journaled_both_keyed_table_is_ready_with_its_journal_key_and_columns():
    assessment = assess()
    assert assessment.ready and assessment.readiness.state == READY
    assert (assessment.journal_library, assessment.journal_name) == ("QDCLIB", "QSQJRN")
    assert assessment.key_columns == ("ID",) and [c["name"] for c in assessment.columns] == ["ID", "NAME", "CREATED"]
    first = assessment.columns[0]
    assert (first["kind"], first["precision"], first["scale"], first["nullable"]) == ("decimal", 9, 0, False)


@pytest.mark.parametrize("kwargs, state, command", [
    ({"journaled": "no"}, NOT_JOURNALED, "STRJRNPF FILE(QDCLIB/ORDERS)"),
    ({"images": "*AFTER"}, IMAGES_INCOMPLETE, "CHGJRNOBJ OBJ((QDCLIB/ORDERS *FILE)) ATR(*IMAGES) IMAGES(*BOTH)"),
    ({"has_key": "no", "keys": ""}, NO_KEY, None),
])
def test_not_ready_tables_are_refused_with_the_state_and_the_exact_cl_commands(kwargs, state, command):
    assessment = assess(**kwargs)
    assert not assessment.ready and assessment.readiness.state == state
    assert (command is None) or any(command in text for text in assessment.cl_commands())
    assert assessment.reasons


def test_a_table_without_a_key_explains_that_rrn_tables_are_out_of_scope():
    assert any("RRN" in reason and "not supported" in reason for reason in assess(has_key="no", keys="").reasons)


def test_unsupported_or_duplicated_columns_make_the_table_not_ready_instead_of_guessing():
    assert not assess(columns="ID,DECIMAL,9,0,no;BLOB1,UNKNOWNTYPE,,,yes").ready
    assert not assess(columns="ID,DECIMAL,9,0,no;ID,DECIMAL,9,0,no").ready
    assert not assess(columns="").ready


def test_find_table_matches_library_and_system_or_sql_name_only():
    rows = disc.parse_discovery(discover_output(table_row(), table_row(library="OTHERLIB")))
    assert disc.find_table(rows, "qdclib", "orders").library == "QDCLIB"
    assert disc.find_table(rows, "QDCLIB", "MISSING") is None


def test_malformed_job_output_is_a_clean_error_not_a_crash():
    for bad in ("", "no prefix here\n", "quadringent_discover_result=not-json\n",
                "quadringent_discover_result=" + json.dumps({"raw_output": "table\tonly\tthree"}) + "\n"):
        with pytest.raises(disc.JobOutputError):
            disc.parse_discovery(bad)
    with pytest.raises(disc.JobOutputError):
        disc.parse_probe("quadringent_probe_result=" + json.dumps({"network": {}}) + "\n")


def test_start_prints_cl_commands_for_a_table_that_is_not_journaled_and_stops_before_any_copy(tmp_path):
    runner = DockerStub()
    runner.table_row = table_row(journaled="no")
    cli = Cli(tmp_path, runner=runner)
    cli.init()
    assert cli.start() == 1
    error = cli.err.getvalue()
    assert "is not journaled" in error and "never changes the IBM i" in error
    assert "  STRJRNPF FILE(QDCLIB/ORDERS)" in error
    assert cli.runner.roles() == ["fs-probe", "probe", "discover"]


def test_start_stops_on_a_table_that_discovery_does_not_find(tmp_path):
    cli = Cli(tmp_path, runner=DockerStub(discover=discover_output(table_row(name="OTHER"))))
    cli.init()
    assert cli.start() == 1 and "was not found" in cli.err.getvalue()


def test_check_discover_reports_readiness_and_prints_the_commands(tmp_path):
    runner = DockerStub()
    runner.table_row = table_row(images="*AFTER")
    cli = Cli(tmp_path, runner=runner)
    cli.init()
    assert cli.invoke("check", "--discover", "--json", connector=None) == 1  # a failed required check blocks
    report = json.loads(cli.out.getvalue())
    item = next(r for r in report["results"] if r["code"] == "source_prerequisites.table")
    assert item["status"] == "failed" and "CHGJRNOBJ" in item["evidence"]["cl_1"]
    assert cli.invoke("check", "--discover") == 1
    assert "CL commands for QDCLIB/ORDERS" in cli.out.getvalue() and "CHGJRNOBJ" in cli.out.getvalue()
