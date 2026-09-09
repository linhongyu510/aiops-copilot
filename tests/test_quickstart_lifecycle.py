"""Tests for the one-click launcher's process lifecycle.

These cover a real defect: `--stop` reported success while the API kept holding
the port, so the next start silently reused a stale build. The fix verifies the
exit instead of assuming SIGTERM worked, and these tests pin that behaviour.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_quickstart():
    """Import quickstart.py by path (it is a top-level script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        "quickstart_under_test", PROJECT_ROOT / "quickstart.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


quickstart = _load_quickstart()


def test_process_alive_reports_current_process() -> None:
    import os

    assert quickstart.process_alive(os.getpid()) is True


def test_process_alive_reports_missing_process() -> None:
    # PID 1 exists, so use an implausibly high PID that cannot be running.
    assert quickstart.process_alive(2**22 - 1) is False


def test_terminate_escalates_when_sigterm_is_ignored(monkeypatch) -> None:
    """A service that ignores SIGTERM must be escalated to SIGKILL."""
    signals_sent: list[int] = []
    alive = {"value": True}

    def fake_kill(pid: int, sig: int) -> None:
        signals_sent.append(sig)
        if sig == quickstart.signal.SIGKILL:
            alive["value"] = False

    monkeypatch.setattr(quickstart.os, "kill", fake_kill)
    monkeypatch.setattr(quickstart, "process_alive", lambda _pid: alive["value"])
    monkeypatch.setattr(quickstart.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(quickstart, "IS_WINDOWS", False)

    assert quickstart.terminate("api", 4242) is True
    assert quickstart.signal.SIGTERM in signals_sent
    assert quickstart.signal.SIGKILL in signals_sent


def test_terminate_stops_at_sigterm_when_process_exits(monkeypatch) -> None:
    signals_sent: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        signals_sent.append(sig)

    monkeypatch.setattr(quickstart.os, "kill", fake_kill)
    monkeypatch.setattr(quickstart, "process_alive", lambda _pid: False)
    monkeypatch.setattr(quickstart.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(quickstart, "IS_WINDOWS", False)

    assert quickstart.terminate("api", 4242) is True
    assert signals_sent == [quickstart.signal.SIGTERM]


def test_terminate_reports_already_gone_process(monkeypatch) -> None:
    def fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(quickstart.os, "kill", fake_kill)
    monkeypatch.setattr(quickstart, "IS_WINDOWS", False)

    assert quickstart.terminate("api", 4242) is False


def test_terminate_warns_when_process_survives(monkeypatch) -> None:
    """If the process cannot be killed, the launcher must not claim success."""
    monkeypatch.setattr(quickstart.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(quickstart, "process_alive", lambda _pid: True)
    monkeypatch.setattr(quickstart.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(quickstart, "IS_WINDOWS", False)

    assert quickstart.terminate("api", 4242) is False


def test_stop_services_removes_stale_pid_files(tmp_path, monkeypatch) -> None:
    """Stale PID files must be cleaned up so later runs do not act on them."""
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    (pid_dir / "api.pid").write_text("999999999", encoding="utf-8")
    (pid_dir / "broken.pid").write_text("not-a-pid", encoding="utf-8")

    monkeypatch.setattr(quickstart, "PID_DIR", pid_dir)
    monkeypatch.setattr(quickstart, "terminate", lambda _name, _pid: False)

    assert quickstart.stop_services() == 0
    assert list(pid_dir.glob("*.pid")) == []


@pytest.mark.parametrize("version", [(3, 9), (3, 10)])
def test_demo_rejects_unsupported_python(monkeypatch, version) -> None:
    """Old interpreters must get a clear message, not an import traceback."""
    monkeypatch.setattr(quickstart, "python_version_of", lambda _python: version)
    assert quickstart.run_demo(Path(sys.executable)) == 1
