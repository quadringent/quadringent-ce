"""The installed collector cannot outlive its outer process deadline."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from quadringent_control_plane.v2.services.native_observer_process import NativeObserverProcessError, run_worker


NORMAL = """
import json,sys
v=json.loads(sys.stdin.readline())
print(json.dumps({"kind":"fence","index":1}),flush=True)
assert json.loads(sys.stdin.readline())=={"current":True,"index":1}
print(json.dumps({"kind":"result","value":{"received":v["scope"]}}),flush=True)
assert json.loads(sys.stdin.readline())=={"finish":True}
"""


def run(code, payload=None, **kw):
    return run_worker(
        (sys.executable, "-u", "-c", code),
        payload or {"scope": "pinned"},
        environment={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        check_current=kw.pop("check_current", lambda: True),
        max_seconds=kw.pop("max_seconds", 3),
        **kw,
    )


def test_worker_checks_context_and_returns_only_after_clean_exit():
    checks = []
    result = run(NORMAL, check_current=lambda: checks.append(True) or True)
    assert result == {"received": "pinned"} and len(checks) >= 3


@pytest.mark.skipif(sys.platform != "linux", reason="Installed Linux guardian import")
def test_installed_worker_does_not_import_the_launcher_from_its_working_directory(tmp_path, monkeypatch):
    (tmp_path / "quadringent_control_plane.py").write_text("raise AssertionError('launcher shadowed package')")
    monkeypatch.chdir(tmp_path)
    assert run(NORMAL) == {"received": "pinned"}


@pytest.mark.parametrize(
    "code",
    [
        'import sys;sys.stdin.readline();print("not-json",flush=True)',
        'import sys;sys.stdin.readline();print("x"*9000000,flush=True)',
        "import sys;sys.stdin.readline();raise SystemExit(2)",
        NORMAL.replace('"index":1', '"index":2'),
        NORMAL.replace('{"kind":"result","value":', '{"kind":"result","extra":true,"value":'),
    ],
)
def test_bad_worker_output_is_redacted(code):
    with pytest.raises(NativeObserverProcessError, match="native_observation_failed"):
        run(code)


def test_blocking_sdk_worker_is_stopped_by_outer_deadline():
    started = time.monotonic()
    with pytest.raises(NativeObserverProcessError):
        run("import time;time.sleep(60)", max_seconds=0.3)
    assert time.monotonic() - started < 2


def test_blocking_context_check_does_not_remove_the_outer_deadline():
    started = time.monotonic()
    with pytest.raises(NativeObserverProcessError):
        run(NORMAL, max_seconds=0.2, check_current=lambda: time.sleep(30))
    assert time.monotonic() - started < 1.5


def test_false_context_never_starts_the_child(tmp_path):
    target = tmp_path / "started"
    with pytest.raises(NativeObserverProcessError):
        run("from pathlib import Path;Path(" + repr(str(target)) + ").touch()", check_current=lambda: False)
    assert not target.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux installed process-tree cleanup")
def test_timeout_kills_a_native_child_that_started_a_separate_session(tmp_path):
    target = tmp_path / "child.pid"
    code = """
import json,subprocess,sys,time
from pathlib import Path
v=json.loads(sys.stdin.readline())
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
Path(v['pid_file']).write_text(str(p.pid))
print(json.dumps({'kind':'fence','index':1}),flush=True)
sys.stdin.readline()
time.sleep(60)
"""
    with pytest.raises(NativeObserverProcessError):
        run(code, {"pid_file": str(target)}, max_seconds=0.7)
    pid = int(target.read_text())
    for _ in range(50):
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists() or stat.read_text().split(") ", 1)[1].split()[0] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("owned native child outlived outer deadline")


def test_configuration_io_is_also_bounded_without_spawning_a_worker():
    from quadringent_control_plane.v2.services.native_observer_process import bounded_call

    started = time.monotonic()
    with pytest.raises(NativeObserverProcessError, match="native_observation_failed"):
        bounded_call(lambda: time.sleep(30), max_seconds=0.1)
    assert time.monotonic() - started < 1


def test_configuration_value_is_returned_without_reflecting_failures():
    from quadringent_control_plane.v2.services.native_observer_process import bounded_call

    assert bounded_call(lambda: {"configured": True}, max_seconds=1) == {"configured": True}

    def broken():
        raise ValueError("SYNTHETIC_PRIVATE_DIAGNOSTIC")

    with pytest.raises(NativeObserverProcessError) as error:
        bounded_call(broken, max_seconds=1)
    assert "SYNTHETIC_PRIVATE_DIAGNOSTIC" not in str(error.value)


def _running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not stat.exists() or stat.read_text().split(") ", 1)[1].split()[0] != "Z"


@pytest.mark.parametrize("normal_exit", [True, False])
@pytest.mark.parametrize("detached", [False, True])
def test_completed_worker_cannot_leave_its_native_child_running(tmp_path, normal_exit, detached):
    if detached and sys.platform != "linux":
        pytest.skip("Linux subreaper adopts detached descendants")
    target = tmp_path / "child.pid"
    code = """
import json,subprocess,sys,time
from pathlib import Path
value=json.loads(sys.stdin.readline())
print(json.dumps({'kind':'fence','index':1}),flush=True)
sys.stdin.readline()
if value['normal_exit']:
 print(json.dumps({'kind':'result','value':{'finished':True}}),flush=True)
 sys.stdin.readline()
time.sleep(0.05)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
 start_new_session=value['detached'],stdin=subprocess.DEVNULL,
 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
Path(value['pid_file']).write_text(str(child.pid))
raise SystemExit(0 if value['normal_exit'] else 9)
"""
    try:
        payload = {"pid_file": str(target), "normal_exit": normal_exit, "detached": detached}
        if normal_exit:
            assert run(code, payload) == {"finished": True}
        else:
            with pytest.raises(NativeObserverProcessError):
                run(code, payload)
        pid = int(target.read_text())
        for _ in range(50):
            if not _running(pid):
                break
            time.sleep(0.01)
        assert not _running(pid), "native descendant survived its completed worker"
    finally:
        if target.exists() and _running(int(target.read_text())):
            os.kill(int(target.read_text()), signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper hard deadline")
def test_guardian_deadline_kills_a_detached_child_without_parent_supervision(tmp_path):
    target = tmp_path / "detached.pid"
    code = """
import subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
 start_new_session=True,stdin=subprocess.DEVNULL,
 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""
    command = (sys.executable, "-P", "-m", "quadringent_control_plane.v2.services.native_observer_guardian",
               "0.3", "--", sys.executable, "-c", code, str(target))
    started = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=2)
        assert process.returncode != 0 and stdout == stderr == b""
        assert time.monotonic() - started < 2
        assert not _running(int(target.read_text()))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        if target.exists() and _running(int(target.read_text())):
            os.kill(int(target.read_text()), signal.SIGKILL)


def test_guardian_cannot_spawn_before_subreaper_confirmation(monkeypatch, capsys):
    from quadringent_control_plane.v2.services import native_observer_guardian as guardian

    def unavailable():
        raise OSError("SYNTHETIC_PRIVATE_KERNEL_DIAGNOSTIC")

    launch = Mock()
    monkeypatch.setattr(guardian, "_subreaper", unavailable)
    monkeypatch.setattr(guardian.subprocess, "Popen", launch)
    assert guardian.supervise((sys.executable, "-c", "pass"), max_seconds=1) == 1
    launch.assert_not_called()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", ["set", "get", "unconfirmed", "proc"])
def test_guardian_requires_kernel_and_process_catalogue_readbacks(monkeypatch, failure):
    import ctypes
    from quadringent_control_plane.v2.services import native_observer_guardian as guardian

    def native(operation, value, *_):
        if operation == 36:
            return -1 if failure == "set" else 0
        ctypes.c_int.from_address(value).value = 0 if failure == "unconfirmed" else 1
        return -1 if failure == "get" else 0

    monkeypatch.setattr(guardian.sys, "platform", "linux")
    monkeypatch.setattr(guardian.ctypes, "CDLL", lambda *a, **kw: type("Library", (), {"prctl": staticmethod(native)})())
    monkeypatch.setattr(guardian, "_identity", lambda _: None if failure == "proc" else "owned-start-tick")
    monkeypatch.setattr(guardian.Path, "read_text", lambda _: "")
    with pytest.raises(ValueError, match="^guardian_unavailable$"):
        guardian._subreaper()
