from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ops_agent.runtime.sandbox import (
    SandboxRunner,
    SandboxUnavailableError,
    platform_true_command,
    register_sandbox_tools,
)
from ops_agent.config import Settings
from ops_agent.runtime.tools import ToolRegistry
from ops_agent.runtime.windows_sandbox import (
    WINDOWS_SAFE_ENV_NAMES,
    windows_safe_env,
    write_roots_for_mode,
)


def test_windows_safe_env_drops_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
    monkeypatch.setenv("POSTGRES_DSN", "postgres://secret")
    monkeypatch.setenv("TEMP", "/tmp")
    env = windows_safe_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "POSTGRES_DSN" not in env
    assert env["PATH"] == "/bin"
    assert set(env) <= set(WINDOWS_SAFE_ENV_NAMES)


def test_write_roots_only_for_workspace_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()
    assert write_roots_for_mode("read-only", tmp_path) == []
    roots = write_roots_for_mode("workspace-write", tmp_path)
    assert tmp_path.resolve() in roots
    assert (tmp_path / "temp").resolve() in roots


def test_restricted_backend_on_this_host(tmp_path: Path) -> None:
    runner = SandboxRunner(tmp_path)
    if sys.platform == "darwin":
        assert runner.restricted_backend in {None, "seatbelt"}
        assert runner.restricted_available is (runner.restricted_backend == "seatbelt")
    elif sys.platform == "win32":
        assert runner.restricted_backend in {None, "appcontainer"}
        assert runner.restricted_available is (runner.restricted_backend == "appcontainer")
    else:
        assert runner.restricted_backend is None
        assert runner.restricted_available is False


def test_restricted_tools_register_only_when_backend_exists(tmp_path: Path) -> None:
    runner = SandboxRunner(tmp_path)
    registry = ToolRegistry()
    settings = Settings(_env_file=None, sandbox_full_access_enabled=False)
    register_sandbox_tools(registry, runner, settings)
    names = registry.tool_names()
    if runner.restricted_available:
        assert "sandbox_read_only" in names
        assert "sandbox_workspace_write" in names
    else:
        assert "sandbox_read_only" not in names
        assert "sandbox_workspace_write" not in names
    assert "sandbox_full_access" not in names


def test_fail_closed_without_restricted_backend(tmp_path: Path) -> None:
    runner = SandboxRunner(tmp_path, timeout_seconds=5)
    runner.restricted_available = False
    runner.restricted_backend = None
    runner._windows_backend = None
    with pytest.raises(SandboxUnavailableError, match="unavailable"):
        runner.run(["echo", "no"], mode="read-only")
    unrestricted = runner.run(platform_true_command(), mode="danger-full-access")
    assert unrestricted.exit_code == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows AppContainer only")
def test_windows_sandbox_read_only_denies_write(tmp_path: Path) -> None:
    runner = SandboxRunner(tmp_path, timeout_seconds=8)
    if runner.restricted_backend != "appcontainer":
        pytest.skip("Windows AppContainer is unavailable")
    cmd = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe"
    readable = runner.run([str(cmd), "/c", "echo ok"], mode="read-only")
    assert readable.exit_code == 0
    assert "ok" in readable.stdout

    denied = runner.run(
        [str(cmd), "/c", "echo blocked>blocked.txt"],
        mode="read-only",
    )
    assert not (tmp_path / "blocked.txt").exists()
    assert denied.exit_code != 0 or "denied" in (denied.stderr + denied.stdout).lower()

    writable = runner.run(
        [str(cmd), "/c", "echo allowed>allowed.txt"],
        mode="workspace-write",
    )
    assert writable.exit_code == 0
    assert (tmp_path / "allowed.txt").exists()
