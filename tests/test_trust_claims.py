"""Claims made on the IBM i footprint and data pages must match the code.

Pages covered:

- docs/product/ibm-i-footprint.md
- docs/product/your-data.md

The checks read the repository only. They do not connect to anything.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FOOTPRINT = ROOT / "docs/product/ibm-i-footprint.md"
YOUR_DATA = ROOT / "docs/product/your-data.md"
PAGES = (FOOTPRINT, YOUR_DATA)

JAVA_MAIN = ROOT / "java/src/main/java/io/quadringent/as400"

# The only Java class allowed to change data or objects on the IBM i. It is
# documented on the footprint page, section "The one exception".
JAVA_ALLOWLIST = frozenset({"QualificationSourceDriver.java"})

# IBM-supplied services read by the Java classes, as listed on the footprint page.
DOCUMENTED_IBMI_SERVICES = frozenset(
    {
        "DISPLAY_JOURNAL",
        "JOURNAL_RECEIVER_INFO",
        "JOURNALED_OBJECTS",
        "IFS_OBJECT_STATISTICS",
        "SYSTABLES",
        "SYSTABLESTAT",
        "SYSCOLUMNS",
        "SYSCST",
        "SYSKEYCST",
        "SYSINDEXES",
        "SYSKEYS",
        "ENV_SYS_INFO",
        "SYSTEM_VALUE_INFO",
        "SYSDUMMY1",
    }
)

# JTOpen classes the Java reader code may import. A new one needs a review of
# what it can do on the IBM i, then an update here and on the page.
ALLOWED_JTOPEN_IMPORTS = frozenset(
    {
        "AS400",
        "AS400DataType",
        "AS400Date",
        "AS400JDBCDataSource",
        "AS400SecurityException",
        "AS400Structure",
        "AS400Text",
        "AS400Time",
        "ReturnCodeException",
        "SecureAS400",
    }
)

MEMBER_IDENTITY_CLASS = "ReadOnlyMemberIdentity.java"
MEMBER_IDENTITY_IMPORTS = frozenset({"ProgramCall", "ProgramParameter", "SocketProperties"})
# Review covers the closed API constructor, exact parameters and call adapter.
MEMBER_IDENTITY_BLOCKS = {
    "    public record ProgramRequest(": "30e4c218928f425ed8f379a78ccb01f763500f1593f25f116ffe38a10bc0bdb3",
    "    private static byte[] member(": "3865f7ccc50733453e7e0fbfce93cfb768ec90f469bddeb8ba4218cc20dfdf3d",
    "    private static void resolve(": "be7422d4cbcc7e10e4969ebecb39c74fb3767d3d2d5f2007e8e3b8d03861cca7",
    "    private static NativeReply call(": "ba20e11e6345c1f08131d444751e2d041c454e71d7817734ca94175cab8842ea",
}

# --------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------

# SQL that changes data or objects. Uppercase verbs at the start of a literal
# catch fragments such as "UPDATE " + table; the lowercase-tolerant shapes
# catch whole statements anywhere in a literal.
_SQL_FRAGMENT_START = re.compile(
    r"^(INSERT INTO|UPDATE|DELETE FROM|MERGE INTO|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|RENAME)\b"
)
_SQL_STATEMENT_SHAPES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\binsert\s+into\s+\S",
        r"\bupdate\s+[\w.\"$#@]+\s+set\b",
        r"\bdelete\s+from\s+\S",
        r"\bmerge\s+into\s+\S",
        r"\btruncate\s+table\b",
        r"\b(drop|alter)\s+(table|view|index|schema|function|procedure|alias|sequence|trigger|variable|type)\b",
        r"\bcreate\s+(or\s+replace\s+)?(table|view|index|schema|function|procedure|alias|sequence|trigger|variable|type)\b",
        r"\bdeclare\s+global\s+temporary\s+table\b",
        r"\b(grant|revoke)\s+\w+\s+on\b",
        r"\brename\s+(table|column)\b",
        r"\b(comment|label)\s+on\b",
        r"\block\s+table\b",
        r"\brefresh\s+table\b",
        r"\bexecute\s+(immediate|procedure)\b",
    )
)
# A call to a stored procedure or to QSYS2.QCMDEXC, in any letter case.
_SQL_CALL = re.compile(r"\bcall\s+[\w.$#@\"]+\s*\(", re.IGNORECASE)

# CL commands: object-changing verbs followed by keyword parameters (uppercase,
# as CL is written), plus explicit command names in any letter case, because
# QCMDEXC accepts lowercase text.
_CL_SYNTAX = re.compile(
    r"\b(?:CRT|DLT|CHG|ADD|RMV|RNM|MOV|CPY|CLR|RGZ|SBM|STR|END|APY|SAV|RST)[A-Z]{2,8}\s+[A-Z][A-Z0-9]*\("
)
_CL_NAMES = re.compile(
    r"\b(?:CRTJRN|CRTJRNRCV|DLTJRN|DLTJRNRCV|CHGJRN|CHGJRNOBJ|STRJRNPF|ENDJRNPF|APYJRNCHG|RMVJRNCHG|"
    r"RGZPFM|CLRPFM|CHGPF|DLTF|CRTPF|CRTLF|SBMJOB|QCMDEXC|QCMD)\b",
    re.IGNORECASE,
)

# Java API surface that can change data, call a command or write to the IFS.
_JAVA_WRITE_API = re.compile(
    r"\b(?:executeUpdate|executeBatch|executeLargeUpdate|executeLargeBatch|prepareCall|CallableStatement|"
    r"CommandCall|ProgramCall|ServiceProgramCall|AS400File|SequentialFile|KeyedFile|DataQueue|"
    r"KeyedDataQueue|UserSpace|SaveFile|IFS[A-Za-z]\w*|addBatch|clearBatch|moveToInsertRow|insertRow|"
    r"updateRow|deleteRow|cancelRowUpdates|CONCUR_UPDATABLE)\b|\.execute\(|\.update[A-Z]\w*\("
)
# Any reference to the JTOpen packages in code (imports included) is checked
# against the allow-list below: a wildcard import, a fully qualified use of a
# class that is not allowed, or another JTOpen package is a finding.
_JTOPEN_REFERENCE = re.compile(r"\bcom\.ibm\.as400\.(\w+)\.(\w+|\*)")
# JTOpen class names that may appear inside string literals (reflective loading
# of the JDBC driver, and the two trust-all system properties that the code
# refuses to honour).
ALLOWED_JTOPEN_LITERALS = frozenset({"AS400JDBCDriver", "SSLTrustAll", "SecureAS400"})
_JAVA_TRUST_BYPASS = re.compile(
    r"\b(?:X509TrustManager|checkServerTrusted|HostnameVerifier|setHostnameVerifier|TrustAllCerts)\b"
)

_IBMI_SERVICE = re.compile(r"\b(?:QSYS2|SYSIBMADM|SYSIBM)\.([A-Z][A-Z0-9_]*)")


def strip_java(source: str) -> tuple[str, list[str]]:
    """Return (code without comments or literals, string literals in order)."""
    code: list[str] = []
    literals: list[str] = []
    i, n = 0, len(source)
    while i < n:
        if source.startswith('"""', i):
            end = source.index('"""', i + 3)
            literals.append(source[i + 3 : end])
            code.append('""')
            i = end + 3
        elif source[i] == '"':
            j = i + 1
            while source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            literals.append(source[i + 1 : j])
            code.append('""')
            i = j + 1
        elif source[i] == "'":
            j = i + 1
            while source[j] != "'":
                j += 2 if source[j] == "\\" else 1
            code.append("' '")
            i = j + 1
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif source.startswith("/*", i):
            i = source.index("*/", i) + 2
        else:
            code.append(source[i])
            i += 1
    return "".join(code), literals


def _normalize(text: str) -> str:
    return " ".join(text.split())


def changing_statements(literals: list[str]) -> list[str]:
    """Literals that look like data-changing SQL or an object-changing CL command."""
    found: list[str] = []
    for literal in literals:
        text = _normalize(literal)
        if not text:
            continue
        if (
            _SQL_FRAGMENT_START.search(text)
            or any(shape.search(text) for shape in _SQL_STATEMENT_SHAPES)
            or _SQL_CALL.search(text)
            or _CL_SYNTAX.search(text)
            or _CL_NAMES.search(text)
        ):
            found.append(text[:80])
    return found


def java_files() -> list[Path]:
    files = sorted(JAVA_MAIN.glob("*.java"))
    assert files, "no Java sources found: the scan would pass vacuously"
    return files


def reader_java_files() -> list[Path]:
    return [path for path in java_files() if path.name not in JAVA_ALLOWLIST]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def reviewed_member_blocks(source: str) -> list[str] | None:
    blocks = []
    for signature, expected in MEMBER_IDENTITY_BLOCKS.items():
        if source.count(signature) != 1:
            return None
        start = source.index(signature)
        end = source.find("\n    }", start)
        if end < 0:
            return None
        block = source[start : end + len("\n    }")]
        if hashlib.sha256(block.encode()).hexdigest() != expected:
            return None
        blocks.append(block)
    return blocks


# --------------------------------------------------------------------------
# (a) Reader code issues no data-changing SQL and no object-changing CL
# --------------------------------------------------------------------------


def reader_source_findings(source: str, *, file_name: str | None = None) -> list[str]:
    """Everything in one Java source that the footprint page says is absent."""
    allowed_imports = ALLOWED_JTOPEN_IMPORTS
    review_findings = []
    if file_name == MEMBER_IDENTITY_CLASS:
        blocks = reviewed_member_blocks(source)
        program_import = "import com.ibm.as400.access.ProgramCall;"
        if blocks is None or source.count(program_import) != 1:
            review_findings.append("unreviewed_member_native_surface")
        else:
            allowed_imports |= MEMBER_IDENTITY_IMPORTS
            source = source.replace(blocks[-1], "").replace(program_import, "")
    code, literals = strip_java(source)
    jtopen = [
        f"com.ibm.as400.{package}.{name}"
        for package, name in _JTOPEN_REFERENCE.findall(code)
        if package != "access" or name not in allowed_imports
    ]
    jtopen_literals = [
        f"com.ibm.as400.{package}.{name}"
        for literal in literals
        for package, name in _JTOPEN_REFERENCE.findall(literal)
        if package != "access" or name not in ALLOWED_JTOPEN_LITERALS
    ]
    return [
        *review_findings,
        *changing_statements(literals),
        *_JAVA_WRITE_API.findall(code),
        *_JAVA_TRUST_BYPASS.findall(code),
        *jtopen,
        *jtopen_literals,
    ]


@pytest.mark.parametrize("path", reader_java_files(), ids=lambda p: p.name)
def test_java_reader_class_issues_no_changing_sql_or_cl(path: Path) -> None:
    assert reader_source_findings(_read(path), file_name=path.name) == []


def test_a_reader_class_with_a_write_would_fail_the_scan() -> None:
    clean = 'class A { void f(java.sql.Statement s) throws Exception { s.executeQuery("SELECT 1 FROM SYSIBM.SYSDUMMY1"); } }'
    assert reader_source_findings(clean) == []
    for body in (
        's.executeUpdate("x");',
        'String q = "DELETE FROM LIB.T WHERE A = 1";',
        'String q = """\n  INSERT INTO LIB.T (A) VALUES (1)\n  """;',
        'String c = "CHGJRN JRN(L/J) JRNRCV(*GEN)";',
        'new com.ibm.as400.access.CommandCall(as400);',
        's.executeQuery("call qsys2.qcmdexc(\'chgjrn jrn(l/j)\')");',
        's.executeQuery("Call QSYS2.QCMDEXC(?, ?)");',
        'r.moveToInsertRow(); r.insertRow();',
        'r.updateString("A", "x"); r.updateRow();',
        's.addBatch("x");',
        'r.deleteRow();',
        'new com.ibm.as400.access.IFSTextFileOutputStream(null, "/x");',
        'new com.ibm.as400.access.CharacterDataArea(null).write("x");',
        'new com.ibm.as400.data.ProgramCallDocument(null);',
        'String q = "LABEL ON TABLE L.T IS \'x\'";',
        'String q = "COMMENT ON TABLE L.T IS \'x\'";',
        'String q = "LOCK TABLE L.T IN EXCLUSIVE MODE";',
        'Class.forName("com.ibm.as400.access.CommandCall");',
    ):
        mutated = f"class A {{ void f(java.sql.Statement s) throws Exception {{ {body} }} }}"
        assert reader_source_findings(mutated), body


def test_wildcard_and_unreviewed_jtopen_imports_are_findings() -> None:
    body = "class A { }"
    assert reader_source_findings(f"import com.ibm.as400.access.*;\n{body}")
    assert reader_source_findings(f"import com.ibm.as400.access.CharacterDataArea;\n{body}")
    assert reader_source_findings(f"import com.ibm.as400.access.AS400;\n{body}") == []


def test_member_native_reads_require_the_reviewed_class_and_narrow_imports() -> None:
    source = _read(JAVA_MAIN / "ReadOnlyMemberIdentity.java")
    assert reader_source_findings(source, file_name="ReadOnlyMemberIdentity.java") == []
    assert reader_source_findings(source, file_name="AnotherReader.java")
    assert reader_source_findings(source)
    assert not {"ProgramCall", "ProgramParameter", "SocketProperties"} & ALLOWED_JTOPEN_IMPORTS


@pytest.mark.parametrize(
    "before,after",
    [
        ('Set.of("QUSRMBRD", "QJORJIDI")', 'Set.of("QUSRMBRD", "QJORJIDI", "QUSRJOB")'),
        ('Set.of("QUSRMBRD", "QJORJIDI")', 'Set.of("QUSRMBRD", "QJORJIDI", "QCMDEXC")'),
        ('new ProgramRequest("QUSRMBRD",', 'new ProgramRequest("QUSRJOB",'),
        ('new ProgramRequest("QUSRMBRD",', 'new ProgramRequest("QCMDEXC",'),
        ('"/QSYS.LIB/" + request.program() + ".PGM"', '"/OTHER.LIB/" + request.program() + ".PGM"'),
        ('if (!Set.of("QUSRMBRD", "QJORJIDI").contains(program) || receiverLength < 1',
         'if (receiverLength < 1'),
        ('ProgramCall call = new ProgramCall(system,', 'ProgramCall call = new ProgramCall(null,'),
        ('encode("0", 1, ccsid), errorBuffer()', 'encode("1", 1, ccsid), errorBuffer()'),
        ('import com.ibm.as400.access.ProgramParameter;', 'import com.ibm.as400.access.*;'),
    ],
    ids=["third-api", "command-api", "third-api-call", "command-call", "changed-path", "removed-validator",
         "changed-adapter", "member-overrides", "wildcard"],
)
def test_member_native_review_rejects_changed_api_guard_path_or_adapter(before: str, after: str) -> None:
    source = _read(JAVA_MAIN / "ReadOnlyMemberIdentity.java")
    assert source.count(before) == 1
    assert reader_source_findings(source.replace(before, after), file_name="ReadOnlyMemberIdentity.java")


@pytest.mark.parametrize(
    "body",
    [
        'new ProgramCall(system, "/QSYS.LIB/QUSRMBRD.PGM", parameters);',
        'new com.ibm.as400.access.ProgramCall(system, "/QSYS.LIB/QUSRMBRD.PGM", parameters);',
        'Class.forName("com.ibm.as400.access.ProgramCall");',
        's.executeUpdate("x");',
        's.executeQuery("CREATE TABLE QTEMP.X (A INT)");',
        's.executeQuery("CALL QSYS2.QCMDEXC(?, ?)");',
        'new com.ibm.as400.access.IFSFileOutputStream(system, "/x");',
        'X509TrustManager trust;',
    ],
)
def test_member_native_review_does_not_hide_other_calls_or_writes(body: str) -> None:
    source = _read(JAVA_MAIN / "ReadOnlyMemberIdentity.java")
    mutated = source.rstrip()[:-1] + f"\n    void unreviewed() {{ {body} }}\n}}\n"
    assert reader_source_findings(mutated, file_name="ReadOnlyMemberIdentity.java")


@pytest.mark.parametrize("name", ["ProgramCall", "ProgramParameter", "SocketProperties", "CommandCall", "CharacterDataArea"])
def test_member_native_imports_are_not_an_unlimited_permission(name: str) -> None:
    source = f"import com.ibm.as400.access.{name};\nclass AnotherReader {{}}"
    assert reader_source_findings(source, file_name="AnotherReader.java")
    if name not in {"ProgramCall", "ProgramParameter", "SocketProperties"}:
        member = _read(JAVA_MAIN / "ReadOnlyMemberIdentity.java")
        assert reader_source_findings(source + member, file_name="ReadOnlyMemberIdentity.java")


def test_member_native_interfaces_are_documented_and_not_product_entrypoints() -> None:
    page = _read(FOOTPRINT)
    for text in ("Unreleased", "`ReadOnlyMemberIdentity`", "`ProgramCall`", "`QUSRMBRD`", "`QJORJIDI`",
                 "MBRD0200", "RJID0100", "Not tested"):
        assert text in page
    for name in ("qusrmbrd.html", "QJORJIDI.html"):
        assert name in page
    for base in ("src", "scripts", "chart", "docker", "deploy"):
        for path in (ROOT / base).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".sh", ".yaml", ".yml", ".tpl", ""}:
                assert "ReadOnlyMemberIdentity" not in _read(path), path


def test_java_reader_classes_import_only_reviewed_jtopen_classes() -> None:
    imported: set[str] = set()
    for path in reader_java_files():
        source = _read(path)
        code, _ = strip_java(source)
        allowed = ALLOWED_JTOPEN_IMPORTS
        if path.name == MEMBER_IDENTITY_CLASS:
            assert reviewed_member_blocks(source) is not None
            allowed |= MEMBER_IDENTITY_IMPORTS
        references = set(_JTOPEN_REFERENCE.findall(code))
        assert all(package == "access" and name in allowed for package, name in references), path.name
        imported.update(name for _, name in references)
    assert imported, "no JTOpen class found: the scan would pass vacuously"


def test_detector_flags_the_allowlisted_class() -> None:
    """The scan is not vacuous: it does find the documented exception."""
    code, literals = strip_java(_read(JAVA_MAIN / "QualificationSourceDriver.java"))
    flagged = " | ".join(changing_statements(literals))
    for expected in ("INSERT INTO", "DELETE FROM", "UPDATE", "CHGJRN", "QCMDEXC"):
        assert expected in flagged
    assert "executeUpdate" in _JAVA_WRITE_API.findall(code)
    assert "prepareCall" in _JAVA_WRITE_API.findall(code)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO LIB.T (A) VALUES (1)",
        "insert into lib.t values (1)",
        "UPDATE LIB.T SET A = 1",
        "DELETE FROM LIB.T WHERE A = 1",
        "MERGE INTO LIB.T USING X ON 1=1",
        "CREATE TABLE QTEMP.X (A INT)",
        "DROP TABLE LIB.T",
        "ALTER TABLE LIB.T ADD COLUMN B INT",
        "CALL QSYS2.QCMDEXC('X', 1)",
        "CHGJRN JRN(LIB/J) JRNRCV(*GEN)",
        "CRTJRNRCV JRNRCV(LIB/R)",
        "STRJRNPF FILE(LIB/F) JRN(LIB/J)",
        "DLTF FILE(LIB/F)",
        "RGZPFM FILE(LIB/F)",
    ],
)
def test_detector_flags_changing_statements(statement: str) -> None:
    assert changing_statements([statement])


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM QSYS2.JOURNALED_OBJECTS WHERE OBJECT_LIBRARY = ?",
        "SELECT SEQUENCE_NUMBER FROM TABLE(QSYS2.DISPLAY_JOURNAL(STARTING_SEQUENCE => 1, ENDING_SEQUENCE => 2))",
        "SELECT T.*, RRN(T) AS \"_rrn\" FROM LIB.T T",
        "retrieve_start receiver=%s start_sequence=%s",
    ],
)
def test_detector_accepts_reads(statement: str) -> None:
    assert changing_statements([statement]) == []


def test_ibmi_services_read_by_java_match_the_documented_list() -> None:
    seen: set[str] = set()
    for path in reader_java_files():
        _, literals = strip_java(_read(path))
        for literal in literals:
            seen.update(_IBMI_SERVICE.findall(literal))
    assert seen == DOCUMENTED_IBMI_SERVICES
    page = _read(FOOTPRINT)
    for service in DOCUMENTED_IBMI_SERVICES:
        assert re.search(rf"\b{service}\b", page), f"{service} is read by the code but missing from the page"


# First word of a literal that starts a SQL or CL statement. Every such literal
# in a reader class must start a read.
_STATEMENT_VERBS = frozenset(
    {
        "SELECT", "WITH", "VALUES", "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER",
        "CREATE", "GRANT", "REVOKE", "RENAME", "CALL", "SET", "LOCK", "COMMENT", "LABEL", "REFRESH",
        "DECLARE", "EXECUTE", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "BEGIN", "CHGJRN", "CRTJRN",
    }
)
_READ_VERBS = frozenset({"SELECT", "WITH", "VALUES"})


def statement_verbs(literals: list[str]) -> list[str]:
    verbs = []
    for literal in literals:
        match = re.match(r"^\s*([A-Za-z]+)\b", literal)
        if match and match.group(1).upper() in _STATEMENT_VERBS:
            verbs.append(match.group(1).upper())
    return verbs


def test_java_sql_literals_are_all_reads() -> None:
    """Every literal that starts with a SQL or CL verb in a reader class starts a read."""
    reads = 0
    for path in reader_java_files():
        _, literals = strip_java(_read(path))
        verbs = statement_verbs(literals)
        assert set(verbs) <= _READ_VERBS, (path.name, sorted(set(verbs) - _READ_VERBS))
        reads += len(verbs)
    assert reads >= 15, "too few SQL reads found: the scan may have stopped seeing them"


def test_capture_session_is_not_read_only_and_snapshot_classes_are() -> None:
    """The footprint page says so; if this changes, update the page."""
    assert "setReadOnly" not in _read(JAVA_MAIN / "JournalSession.java")
    for name in ("ReadOnlyTableSnapshot.java", "FleetCatalogProbe.java"):
        assert "setReadOnly(true)" in _read(JAVA_MAIN / name)
    page = _read(FOOTPRINT)
    assert "never calls `setReadOnly`" in page


# Python side -------------------------------------------------------------


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(_read(path))
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_python_ibmi_adapter_holds_only_reads() -> None:
    path = ROOT / "src/quadringent/ibmi_reader.py"
    constants = _string_constants(path)
    assert changing_statements(constants) == []
    selects = [c for c in constants if re.match(r"^\s*SELECT\b", c)]
    assert len(selects) >= 3
    text = _read(path)
    assert not re.search(r"\.(executemany|commit)\(", text)
    assert text.count(".execute(") == 1  # the single read helper


def test_ibmi_python_adapter_is_created_only_by_two_developer_scripts() -> None:
    creators = set()
    for base in ("src", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            if "IbmiJournalReader(" in _read(path) and path.name != "ibmi_reader.py":
                creators.add(path.relative_to(ROOT).as_posix())
    assert creators == {"scripts/as400_journal_probe.py", "scripts/as400_reader_benchmark.py"}
    dockerfile = _read(ROOT / "docker/Dockerfile")
    for script in creators:
        assert script not in dockerfile


def test_odbc_is_imported_only_by_those_scripts_and_never_executes_sql_directly() -> None:
    importers = set()
    for base in ("src", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            text = _read(path)
            if re.search(r"^\s*(import|from)\s+(pyodbc|ibm_db|jaydebeapi|jpype)\b", text, re.MULTILINE):
                importers.add(path.relative_to(ROOT).as_posix())
                assert ".execute(" not in text
    assert importers == {"scripts/as400_journal_probe.py", "scripts/as400_reader_benchmark.py"}


def test_no_python_module_sends_a_cl_command() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        text = _read(path)
        assert "QCMDEXC" not in text, path
        assert "CommandCall" not in text, path


def test_cl_commands_are_suggestions_stored_for_display_only() -> None:
    consumers = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if re.search(r"cl_commands|cl_fix_commands|ClCommand", _read(path))
    }
    assert consumers == {
        "src/quadringent/table_discovery.py",
        "src/quadringent_control_plane/v2/schema.py",
        "src/quadringent_control_plane/v2/migrations/versions/0004_tables_discovery.py",
        "src/quadringent_control_plane/v2/services/tables.py",
        # quadringent local prints the same suggestions for the administrator to review.
        "src/quadringent/local/check.py",
        "src/quadringent/local/cli.py",
        "src/quadringent/local/discovery.py",
        "src/quadringent/local/orchestrator.py",
    }
    for name in ("cli.py", "orchestrator.py"):
        text = _read(ROOT / "src/quadringent/local" / name)
        assert 'print(f"  {command}"' in text, name
    for name in ("check.py", "cli.py", "discovery.py", "orchestrator.py"):
        text = _read(ROOT / "src/quadringent/local" / name)
        assert not re.search(r"QCMDEXC|CommandCall|runner\.run\([^)]*command\b", text), name
    tree = ast.parse(_read(ROOT / "src/quadringent/table_discovery.py"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= {"__future__", "dataclasses", "re"}


def _sample_table(**overrides):
    from quadringent.table_discovery import DiscoveredTable

    values = dict(
        library="DEMOLIB", system_name="ORDERS", sql_name="ORDERS", text=None, row_count=1, size_bytes=1,
        has_key=True, key_columns=("ID",), journaled=False, journal_library=None, journal_name=None,
        images=None, omitted=False,
    )
    values.update(overrides)
    return DiscoveredTable(**values)


def test_suggested_cl_commands_match_the_footprint_page() -> None:
    from quadringent.table_discovery import classify_table

    commands: list[str] = []
    for kwargs in (
        {"has_journal_in_library": False},
        {"has_journal_in_library": True},
        {"existing_journal": ("DEMOLIB", "DEMOJRN")},
    ):
        commands.extend(c.command for c in classify_table(_sample_table(), **kwargs).cl_commands)
    commands.extend(
        c.command
        for c in classify_table(
            _sample_table(journaled=True, journal_library="DEMOLIB", journal_name="DEMOJRN", images="*AFTER")
        ).cl_commands
    )
    verbs = {command.split()[0] for command in commands}
    assert verbs == {"CRTJRNRCV", "CRTJRN", "STRJRNPF", "CHGJRNOBJ"}
    page = _read(FOOTPRINT)
    for verb in verbs:
        assert f"`{verb} " in page
    assert "IMAGES(*BOTH) OMTJRNE(*OPNCLO)" in page
    assert "MNGRCV(*SYSTEM) DLTRCV(*NO)" in page


# The documented exception ------------------------------------------------

_WRITE_MODES = ("exec", "exec-timed", "freshness-worker", "rotate")


def test_qualification_driver_is_referenced_only_by_qualification_tooling() -> None:
    referencing = set()
    for base in ("src", "scripts", "chart", "deploy", "docker"):
        for path in (ROOT / base).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".sh", ".yaml", ".yml", ".tpl", ".tf", ".json", ""}:
                try:
                    text = _read(path)
                except UnicodeDecodeError:
                    continue
                if "QualificationSourceDriver" in text:
                    referencing.add(path.relative_to(ROOT).as_posix())
    assert referencing == {
        "src/quadringent_qualification/real_source.py",
        "src/quadringent/qualification/collect.py",
        "src/quadringent/qualification/proof.py",
    }
    # The package shipped in the wheel only uses the read-only modes.
    for name in ("collect.py", "proof.py"):
        text = _read(ROOT / "src/quadringent/qualification" / name)
        modes = re.findall(r"QualificationSourceDriver['\"]\s*,\s*['\"]([a-z-]+)['\"]", text)
        assert modes and set(modes) <= {"dump", "rowpos"}, (name, modes)


def test_write_modes_are_wired_only_in_the_source_tree_qualification_harness() -> None:
    harness = _read(ROOT / "src/quadringent_qualification/real_source.py")
    assert 'self._run("exec"' in harness and 'self._run("rotate")' in harness
    pyproject = tomllib_load(ROOT / "pyproject.toml")
    includes = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
    assert not any(item.startswith("quadringent_qualification") for item in includes)


DIAGNOSTIC_JAVA_CLASSES = ("JournalDlScan", "JournalWindowOracle", "MultiObjectDisplayJournal")


def test_diagnostic_java_classes_are_not_started_by_a_product_entry_point() -> None:
    """The footprint page says no job or image entry point starts these classes."""
    referencing: dict[str, set[str]] = {name: set() for name in DIAGNOSTIC_JAVA_CLASSES}
    for base in ("src", "scripts", "chart", "docker", "deploy", "infra-values", ".github"):
        root = ROOT / base
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or any(part.endswith(".egg-info") for part in path.parts) or "__pycache__" in path.parts:
                continue
            try:
                text = _read(path)
            except UnicodeDecodeError:
                continue
            for name in DIAGNOSTIC_JAVA_CLASSES:
                if name in text:
                    referencing[name].add(path.relative_to(ROOT).as_posix())
    assert referencing["JournalDlScan"] == set()
    assert referencing["MultiObjectDisplayJournal"] == set()
    assert referencing["JournalWindowOracle"] <= {"scripts/test_java_journal_oracle.sh"}


def test_row_image_class_hashes_what_it_reads_and_metadata_classes_do_not_select_it() -> None:
    row_image = _read(JAVA_MAIN / "MultiObjectDisplayJournal.java")
    assert "HEX(ENTRY_DATA)" in row_image and "sha256Hex" in row_image
    for name in ("JournalDlScan.java", "JournalWindowOracle.java"):
        assert "ENTRY_DATA" not in _read(JAVA_MAIN / name), name
    page = _read(FOOTPRINT)
    assert "HEX(ENTRY_DATA)" in page and "`MultiObjectDisplayJournal.java`" in page


def test_product_refuses_ibmi_plaintext_in_chart_installer_and_onboarding() -> None:
    chart = _read(ROOT / "chart/templates/deployment.yaml")
    assert "as400.allowPlaintext must remain false" in chart
    assert "as400.tls must remain true" in chart
    assert '"allowPlaintext": False' in _read(ROOT / "src/quadringent/installer/plan.py")
    onboarding = _read(ROOT / "src/quadringent_control_plane/onboarding.py")
    assert "tls is not True or allow_plaintext is True" in onboarding
    assert "the chart and installer refuse plaintext" in _read(ROOT / "SECURITY.md")
    assert "the product refuses plaintext" in _read(FOOTPRINT)


# The capture image ships two side-effect-free boundary helpers from quadringent/qualification
# (data classes and validation). The write-capable qualification driver stays out.
SHIPPED_QUALIFICATION_HELPERS = ("src/quadringent/qualification/__init__.py", "src/quadringent/qualification/boundary.py")


def test_capture_image_does_not_copy_qualification_code() -> None:
    dockerfile = _read(ROOT / "docker/Dockerfile")
    assert "quadringent_qualification" not in dockerfile
    assert "QualificationSourceDriver" not in dockerfile
    shipped = set(re.findall(r"src/quadringent/qualification/[\w./-]+", dockerfile))
    assert shipped <= set(SHIPPED_QUALIFICATION_HELPERS), shipped
    for helper in SHIPPED_QUALIFICATION_HELPERS:
        tree = ast.parse(_read(ROOT / helper))
        imported = {(node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert all(name.startswith(("__future__", "dataclasses", "quadringent")) for name in imported), (helper, imported)
        assert not re.search(r"subprocess|socket|jaydebeapi|pyodbc|QCMDEXC|CommandCall|INSERT|UPDATE|DELETE|CHGJRN", _read(ROOT / helper)), helper
    assert "quadringent_lab" not in dockerfile
    runtime = _read(ROOT / "docker/runtime-modules.txt")
    assert "qualification" not in runtime
    assert "quadringent_lab" not in runtime


def tomllib_load(path: Path) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(_read(path))
    pytest.skip("tomllib needs Python 3.11 or later")


# No IBM i artefact is shipped -----------------------------------------------

_IBMI_SOURCE_SUFFIXES = {
    ".rpgle", ".sqlrpgle", ".rpg", ".clle", ".clp", ".cblle", ".cbl", ".savf", ".dspf", ".prtf", ".pf", ".lf",
}


def test_repository_contains_no_ibmi_source_or_save_file() -> None:
    found = []
    for base in ("src", "java", "scripts", "deploy", "chart", "docker", "docs", "examples", "infra-values"):
        for path in (ROOT / base).rglob("*"):
            if path.is_file() and path.suffix.lower() in _IBMI_SOURCE_SUFFIXES:
                found.append(path.relative_to(ROOT).as_posix())
    assert found == []


# --------------------------------------------------------------------------
# TLS, ports and credentials stated on the footprint page
# --------------------------------------------------------------------------


def test_documented_ports_match_the_probe_code() -> None:
    from quadringent.diagnostics.network import DEFAULT_TLS_PORTS, SERVER_MAPPER_PORT

    assert DEFAULT_TLS_PORTS == {"database": 9471, "signon": 9476, "command": 9475}
    assert SERVER_MAPPER_PORT == 449
    page = _read(FOOTPRINT)
    for port in (*DEFAULT_TLS_PORTS.values(), SERVER_MAPPER_PORT):
        assert re.search(rf"\|\s*{port}\s*\|", page), port


def test_tls_is_on_by_default_and_plaintext_needs_two_settings() -> None:
    session = _read(JAVA_MAIN / "JournalSession.java")
    assert 'tlsValue("AS400_TLS", true)' in session
    assert 'booleanValue("AS400_ALLOW_PLAINTEXT", false)' in session
    values = yaml.safe_load(_read(ROOT / "chart/values.yaml"))
    assert values["as400"]["tls"] is True
    assert values["as400"]["allowPlaintext"] is False
    trust = _read(JAVA_MAIN / "TlsTrust.java")
    assert "There is no trust-all path." in trust
    assert "PRIVATE KEY" in trust


def test_ibmi_password_comes_from_the_environment_and_secret_names_match_the_page() -> None:
    session = _read(JAVA_MAIN / "JournalSession.java")
    assert 'required("ISERIES_PASSWORD")' in session
    from quadringent_control_plane.v2.executor.manifests import ibmi_secret_ref
    from quadringent_control_plane.v2.executor.secrets_provisioner import KEY_ISERIES_PASSWORD

    assert ibmi_secret_ref("abc") == "qdt-source-abc"
    assert KEY_ISERIES_PASSWORD == "ISERIES_PASSWORD"
    crypto = _read(ROOT / "src/quadringent_control_plane/v2/crypto.py")
    assert "from cryptography.fernet import Fernet" in crypto
    for page in PAGES:
        text = _read(page)
        assert "qdt-source-" in text and "ISERIES_PASSWORD" in text


# --------------------------------------------------------------------------
# (b) Telemetry is disabled by default
# --------------------------------------------------------------------------


def test_telemetry_config_is_disabled_by_default() -> None:
    from quadringent_control_plane.telemetry import TelemetryConfig, maybe_start

    assert TelemetryConfig().enabled is False
    assert TelemetryConfig.from_env({}).enabled is False
    for value in ("", "0", "false", "off", "no", "disabled", "maybe"):
        assert TelemetryConfig.from_env({"QUADRINGENT_TELEMETRY": value}).enabled is False, value
    for value in ("1", "true", "TRUE", "on", "yes"):
        assert TelemetryConfig.from_env({"QUADRINGENT_TELEMETRY": value}).enabled is True, value
    assert maybe_start(TelemetryConfig(), version="0", table_count=0) is None


def test_disabled_telemetry_opens_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import quadringent_control_plane.telemetry as telemetry

    def refuse(*args, **kwargs):
        raise AssertionError("telemetry must not send while disabled")

    monkeypatch.setattr(telemetry, "send_once", refuse)
    monkeypatch.setattr(telemetry._request, "urlopen", refuse)
    assert telemetry.maybe_start(telemetry.TelemetryConfig(), version="0", table_count=3) is None


def test_cli_telemetry_is_off_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_text = _read(ROOT / "src/quadringent_control_plane/cli.py")
    assert re.search(r'"--telemetry",\s*default=os\.environ\.get\("QUADRINGENT_TELEMETRY", "off"\)', cli_text)
    monkeypatch.delenv("QUADRINGENT_TELEMETRY", raising=False)
    monkeypatch.delenv("QUADRINGENT_TELEMETRY_URL", raising=False)
    from quadringent_control_plane.cli import _start_telemetry

    off = argparse.Namespace(telemetry="off", telemetry_url="", fleet_catalog="")
    assert _start_telemetry(off) is None
    unset_default = argparse.Namespace(telemetry="", telemetry_url="", fleet_catalog="")
    assert _start_telemetry(unset_default) is None


def test_chart_sets_the_telemetry_variable_only_when_enabled() -> None:
    values = yaml.safe_load(_read(ROOT / "chart/values.yaml"))
    assert values["controlPlane"]["telemetry"]["enabled"] is False
    assert values["controlPlane"]["telemetry"]["endpoint"] == ""
    lines = _read(ROOT / "chart/templates/control-plane.yaml").splitlines()
    variable_lines = [i for i, line in enumerate(lines) if "name: QUADRINGENT_TELEMETRY" in line and "_URL" not in line]
    assert len(variable_lines) == 1
    guard = "{{- if .Values.controlPlane.telemetry.enabled }}"
    assert guard in "\n".join(lines[max(0, variable_lines[0] - 2) : variable_lines[0]])


def test_only_the_cli_starts_telemetry_and_the_endpoint_is_defined_once() -> None:
    importers = set()
    endpoint_files = set()
    for path in (ROOT / "src").rglob("*.py"):
        text = _read(path)
        if re.search(r"quadringent_control_plane\.telemetry|from \.telemetry import|from \. import telemetry", text):
            importers.add(path.relative_to(ROOT).as_posix())
        if "telemetry.quadringent.io" in text:
            endpoint_files.add(path.relative_to(ROOT).as_posix())
    assert importers == {"src/quadringent_control_plane/cli.py"}
    assert endpoint_files == {"src/quadringent_control_plane/telemetry.py"}
    assert "telemetry.quadringent.io/v1/ping" in _read(YOUR_DATA)


# Files that open an HTTP connection through the standard library or httpx,
# with what each one talks to. This is a review list: a new file here needs an
# entry in the outbound table of docs/product/your-data.md. Network clients of
# third-party SDKs (cloud SDKs, the Snowflake connector, JTOpen) are not scanned.
HTTP_CLIENT_FILES = {
    "src/quadringent_control_plane/k8s_jobs.py": "Kubernetes API of the cluster",
    "src/quadringent_control_plane/k8s_pods.py": "Kubernetes API of the cluster",
    "src/quadringent_control_plane/repository.py": "document origins set by the operator (console, proof, fleet)",
    "src/quadringent_control_plane/server.py": "loopback upstream of the control plane process",
    "src/quadringent_control_plane/telemetry.py": "telemetry endpoint, opt-in",
    "src/quadringent_control_plane/v2/mcp_server.py": "control-plane API (in process or the URL given)",
    "src/quadringent/alert_transport.py": "alert webhook configured by the operator",
    "src/quadringent/cost_collect.py": "public AWS price list and the OpenCost URL given",
    "src/quadringent/installer/api_client.py": "control-plane API URL given on the command line",
    "src/quadringent/installer/doctor.py": "control-plane API URL given on the command line",
    "src/quadringent/installer/mcp_bridge.py": "control-plane API URL given on the command line",
    "src/quadringent/qualification/cli.py": "control-plane API URL given on the command line",
    "src/quadringent_lab/server.py": "loopback, local laboratory",
    "scripts/verify_quadringent_vertical.py": "loopback, repository check",
}
_HTTP_CLIENT_MODULES = {"httpx", "requests", "urllib3", "aiohttp", "http.client", "urllib.request"}

# Hosts written as literal URLs in the code. Documentation links and build or
# schema identifiers are listed so that a new host is a deliberate decision.
DOCUMENTED_URL_HOSTS = {
    "telemetry.quadringent.io": "opt-in telemetry endpoint",
    "pricing.us-east-1.amazonaws.com": "public AWS price list, cost collector",
    "www.googleapis.com": "OAuth scope name in qualification code, not a request target",
    "www.ibm.com": "documentation links",
    "github.com": "release and repository links",
    "raw.githubusercontent.com": "site build",
    "pypi.org": "notices collection, release tooling",
    "schema.org": "site build metadata",
    "www.sitemaps.org": "site build metadata",
    "mcp.internal": "placeholder host",
}
_IGNORED_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|[\w.-]*\.(svc|local|invalid|test|example)|example\.(com|org|net))$")
_URL_HOST = re.compile(r"https?://([A-Za-z0-9.\\-]+)")  # a backslash covers regex-escaped dots


def _python_files() -> list[Path]:
    files = [
        path
        for base in ("src", "scripts")
        for path in (ROOT / base).rglob("*.py")
        if not any(part.endswith(".egg-info") for part in path.parts)
    ]
    assert files
    return sorted(files)


def http_client_files() -> set[str]:
    found = set()
    for path in _python_files():
        for node in ast.walk(ast.parse(_read(path))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            if any(name in _HTTP_CLIENT_MODULES for name in names):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def literal_url_hosts() -> set[str]:
    hosts = set()
    for path in _python_files():
        for constant in _string_constants(path):
            for host in _URL_HOST.findall(constant):
                host = host.replace("\\", "").rstrip(".")
                if not _IGNORED_HOST.match(host):
                    hosts.add(host)
    return hosts


def test_http_client_call_sites_are_the_documented_ones() -> None:
    assert http_client_files() == set(HTTP_CLIENT_FILES)
    page = _read(YOUR_DATA)
    for needle in ("Kubernetes API", "telemetry endpoint", "price list", "control-plane API"):
        assert needle in page, needle
    assert "machine-checked" in page


def test_literal_url_hosts_are_the_documented_ones() -> None:
    assert literal_url_hosts() <= set(DOCUMENTED_URL_HOSTS)
    quadringent_hosts = {host for host in literal_url_hosts() if host.endswith("quadringent.io")}
    assert quadringent_hosts == {"telemetry.quadringent.io"}


def test_telemetry_payload_matches_the_documentation() -> None:
    from quadringent_control_plane.telemetry import TelemetryConfig, build_payload

    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        payload = build_payload(TelemetryConfig(state_dir=Path(directory)), version="0.0.0", table_count=2, now=1.0)
    documented = re.search(r"```json\n(\{.*?\})\n```", _read(ROOT / "docs/product/telemetry.md"), re.DOTALL)
    assert documented
    assert set(json.loads(documented.group(1))) == set(payload)
    assert payload["tier"] == "community"


def test_no_code_reads_a_license_key() -> None:
    for base in ("src", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            assert "QUADRINGENT_LICENSE_KEY" not in _read(path), path


def test_terraform_modules_create_no_static_access_key() -> None:
    for path in (ROOT / "deploy/terraform").rglob("*.tf"):
        text = _read(path)
        assert "aws_iam_access_key" not in text, path
        assert "google_service_account_key" not in text, path


# --------------------------------------------------------------------------
# (c) Links and cited sources resolve
# --------------------------------------------------------------------------

_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
_SOURCE_COMMENT = re.compile(r"<!--\s*source:\s*(.*?)\s*-->", re.DOTALL)


def _slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[`*]", "", text)
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    in_fence = False
    anchors: set[str] = set()
    for line in _read(path).splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
            if match:
                anchors.add(_slug(match.group(1)))
    return anchors


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pages_exist(page: Path) -> None:
    assert page.is_file()
    assert _read(page).startswith("# ")


def broken_links(page: Path, root: Path = ROOT) -> list[str]:
    broken = []
    for target in _LINK.findall(_read(page)):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue  # external URL
        path_part, _, fragment = target.partition("#")
        resolved = page if not path_part else (page.parent / path_part).resolve()
        if not resolved.exists() or (root not in resolved.parents and resolved != root):
            broken.append(target)
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(target)
    return broken


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_relative_link_resolves(page: Path) -> None:
    assert _LINK.findall(_read(page)), "the page has no links: the check would pass vacuously"
    assert broken_links(page) == []


def test_link_check_catches_a_missing_file_and_a_missing_anchor(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# Other\n\n## Real heading\n", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text(
        "[ok](other.md#real-heading) [gone](missing.md) [bad anchor](other.md#nope) "
        "[outside](../../etc/passwd) [web](https://example.org/x)\n",
        encoding="utf-8",
    )
    assert broken_links(page, root=tmp_path) == ["missing.md", "other.md#nope", "../../etc/passwd"]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_source_comment_names_existing_files(page: Path) -> None:
    comments = _SOURCE_COMMENT.findall(_read(page))
    assert comments, "the page cites no source: every claim needs one"
    for comment in comments:
        for token in (item.strip() for item in comment.split(",")):
            assert token and (ROOT / token).exists(), f"{page.name}: source not found: {token!r}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_backticked_repository_paths_exist(page: Path) -> None:
    text = _read(page)
    for token in set(re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|java|md|yaml|yml|tf|json|txt|sh))`", text)):
        if "/" in token:
            assert (ROOT / token).exists() or (page.parent / token).exists(), f"{page.name}: no such path {token}"
        elif token.endswith(".java"):
            assert (JAVA_MAIN / token).exists(), f"{page.name}: no such Java file {token}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pages_carry_no_private_identity(page: Path) -> None:
    from check_publication import scan_text

    text = _read(page)
    assert scan_text(text, page.name) == []
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "no email address belongs on these pages"
    assert "/Users/" not in text and "/home/" not in text
