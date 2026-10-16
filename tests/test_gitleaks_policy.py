"""The pinned scanner exempts reviewed fixtures without hiding new leaks."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / ".gitleaks.toml"
VERSION = "8.30.1"


@dataclass(frozen=True)
class Case:
    name: str
    path: str
    rule: str
    selector: str


CASES = (
    Case("layout", "src/quadringent/local/config.py", "generic-api-key", "layout"),
    Case("source-canary", "src/quadringent_lab/fixtures.py", "generic-api-key", "CANARY_SECRET"),
    Case("evidence-canary", "src/quadringent_lab/fixtures.py", "generic-api-key", "CANARY_EVIDENCE_SECRET"),
    Case("fake-pem", "tests/local_cli_support.py", "private-key", "pem"),
    Case("jwt-canary", "tests/test_diagnostics_export.py", "jwt", "jwt"),
    Case("browser-canary", "ui/scripts/lab-browser-scenarios.mjs", "generic-api-key", "browser"),
)


@pytest.fixture(scope="module")
def scanner():
    executable = shutil.which("gitleaks")
    if executable is None:
        pytest.skip("Gitleaks is installed by CI before the policy tests")
    version = subprocess.run([executable, "version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == VERSION, "policy evidence requires the pinned scanner version"
    return executable


def stage(tmp_path):
    root = tmp_path / "source"
    for path in {case.path for case in CASES}:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    return root


def scan(scanner, root):
    report = root.parent / "findings.json"
    result = subprocess.run(
        [scanner, "dir", str(root), "--config", str(POLICY), "--redact=100", "--no-banner",
         "--report-format", "json", "--report-path", str(report)],
        cwd=root, capture_output=True, text=True,
    )
    assert result.returncode in (0, 1), "scanner failed before producing policy evidence"
    findings = json.loads(report.read_text())
    assert result.returncode == (1 if findings else 0)
    return [{"rule": item["RuleID"], "file": str(Path(item["File"]).relative_to(root))} for item in findings]


def assignment_literal(text, name):
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name
                                               for target in node.targets):
            return node.value
    raise AssertionError("fixture assignment is missing")


def changed_fixture(case, text):
    if case.selector == "layout":
        layout = ast.get_docstring(ast.parse(text))
        assert layout is not None
        name = next(word for word in layout.split() if word.endswith(".p8"))
        return text.replace(name, name + "Z", 1)
    if case.selector == "browser":
        fixtures = (ROOT / "src/quadringent_lab/fixtures.py").read_text()
        literal = assignment_literal(fixtures, "CANARY_EVIDENCE_SECRET")
        value = ast.literal_eval(literal)
        assert value in text
        return text.replace(value, value + "Z", 1)
    if case.selector == "pem":
        literal = next(node for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Constant)
                       and isinstance(node.value, str) and "BEGIN PRIVATE KEY" in node.value)
        value = literal.value
        lines = value.splitlines(keepends=True)
        lines[1] = ("Z" if lines[1][0] != "Z" else "Y") + lines[1][1:]
        changed = "".join(lines)
    elif case.selector == "jwt":
        mapping = assignment_literal(text, "CANARIES")
        assert isinstance(mapping, ast.Dict)
        literal = next(value for key, value in zip(mapping.keys, mapping.values)
                       if isinstance(key, ast.Constant) and key.value == "jwt")
        changed = ast.literal_eval(literal) + "Z"
    else:
        literal = assignment_literal(text, case.selector)
        changed = ast.literal_eval(literal) + "Z"
    original = ast.get_source_segment(text, literal)
    assert original is not None
    return text.replace(original, repr(changed), 1)


def fixture_fragment(case, text):
    lines = text.splitlines()
    if case.selector == "layout":
        return next(line for line in lines if line.lstrip().startswith("secrets/"))
    if case.selector == "browser":
        fixtures = (ROOT / "src/quadringent_lab/fixtures.py").read_text()
        value = ast.literal_eval(assignment_literal(fixtures, "CANARY_EVIDENCE_SECRET"))
        return next(line for line in lines if value in line)
    if case.selector == "pem":
        literals = [node for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)]
        private = next(node for node in literals if "BEGIN PRIVATE KEY" in node.value)
        public = next(node for node in literals if "BEGIN PUBLIC KEY" in node.value)
        return "\n".join(lines[private.lineno - 1:public.end_lineno])
    if case.selector == "jwt":
        mapping = assignment_literal(text, "CANARIES")
        assert isinstance(mapping, ast.Dict)
        literal = next(value for key, value in zip(mapping.keys, mapping.values)
                       if isinstance(key, ast.Constant) and key.value == "jwt")
    else:
        literal = assignment_literal(text, case.selector)
    return lines[literal.lineno - 1]


def test_reviewed_fixture_sources_pass_the_pinned_scanner(scanner, tmp_path):
    assert scan(scanner, stage(tmp_path)) == []


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_changed_fixture_in_the_same_assignment_is_detected(scanner, tmp_path, case):
    root = stage(tmp_path)
    path = root / case.path
    changed = changed_fixture(case, path.read_text())
    assert changed != path.read_text()
    path.write_text(changed)
    assert {"rule": case.rule, "file": case.path} in scan(scanner, root)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_another_secret_in_the_same_file_is_detected(scanner, tmp_path, case):
    root = stage(tmp_path)
    path = root / case.path
    # Balance the canary alphabet to make entropy filtering deterministic.
    canary = "".join(format(index, "x") for index in range(16)) * 4
    text = "\napi_key = " + repr(canary) + "\n"
    control = root / "unapproved-control.txt"
    control.write_text(text)
    assert {"rule": "generic-api-key", "file": control.name} in scan(scanner, root)
    control.unlink()
    with path.open("a") as stream:
        stream.write(text)
    assert {"rule": "generic-api-key", "file": case.path} in scan(scanner, root)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_reviewed_canary_escaping_its_source_file_is_detected(scanner, tmp_path, case):
    root = stage(tmp_path)
    leaked = "exports/" + Path(case.path).name
    destination = root / leaked
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(fixture_fragment(case, (root / case.path).read_text()))
    assert {"rule": case.rule, "file": leaked} in scan(scanner, root)


def test_the_documented_layout_exception_also_matches_its_exact_wheel_path(scanner, tmp_path):
    root = stage(tmp_path)
    source = root / CASES[0].path
    wheel = root / "quadringent/local/config.py"
    wheel.parent.mkdir(parents=True)
    shutil.move(source, wheel)
    assert scan(scanner, root) == []


def test_new_fixture_exceptions_require_exact_path_rule_and_match():
    policy = tomllib.loads(POLICY.read_text())
    reviewed = [item for item in policy["allowlists"] if item["description"].startswith("Reviewed ")]
    assert len(reviewed) == len(CASES)
    for item in reviewed:
        assert item["condition"] == "AND"
        assert len(item["targetRules"]) == 1
        assert item["targetRules"][0] in {case.rule for case in CASES}
        assert item["regexTarget"] == "match"
        assert len(item["regexes"]) == 1 and item["regexes"][0].startswith("^") and item["regexes"][0].endswith("$")
        assert item["paths"] and all(path.endswith("$") and ".*" not in path for path in item["paths"])
    assert policy["extend"]["useDefault"] is True
    license_rule = next(item for item in policy["rules"] if item["id"] == "quadringent-license")
    assert license_rule["description"] == "Plaintext Quadringent commercial license"
    assert license_rule["keywords"] == ["quadringent1"]
    assert license_rule["regex"] == r"quadringent1[.][A-Za-z0-9_-]{40,}[.][A-Za-z0-9_-]{60,}"


def test_the_custom_license_rule_remains_active_in_an_allowlisted_source_file(scanner, tmp_path):
    root = stage(tmp_path)
    token = ".".join(("quadringent1", secrets.token_urlsafe(40), secrets.token_urlsafe(60)))
    with (root / CASES[0].path).open("a") as stream:
        stream.write("\nlicense = " + repr(token) + "\n")
    assert {"rule": "quadringent-license", "file": CASES[0].path} in scan(scanner, root)
