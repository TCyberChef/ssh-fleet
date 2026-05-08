"""Tests for MCP-facing remote file/script tools."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from ssh_fleet import server
from ssh_fleet.machines import MachineStore
from ssh_fleet.ssh import CommandResult, ScriptRunResult, SSHConnectionError


@pytest.fixture
def server_env(monkeypatch):
    store = MachineStore()
    pool = MagicMock()
    pool.write_file = AsyncMock()
    pool.run_script = AsyncMock()
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "pool", pool)
    return {"store": store, "pool": pool}


@pytest.mark.asyncio
async def test_write_file_unknown_host_returns_error(server_env):
    result = await server.write_file("ghost", "/tmp/example.txt", "hello")

    assert result == (
        "ERROR: Machine 'ghost' not found."
        " Use list_machines to see available machines."
    )
    server_env["pool"].write_file.assert_not_called()


@pytest.mark.asyncio
async def test_write_file_success_reports_path_and_bytes(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].write_file.return_value = 12

    result = await server.write_file(
        "web-1",
        "/etc/example.conf",
        "hello world\n",
        sudo=True,
        mode="0640",
        owner="root:root",
    )

    assert result == "Wrote 12 bytes to web-1:/etc/example.conf"
    server_env["pool"].write_file.assert_awaited_once()
    call = server_env["pool"].write_file.await_args
    assert call.args[1] == "/etc/example.conf"
    assert call.args[2] == "hello world\n"
    assert call.kwargs["sudo"] is True
    assert call.kwargs["mode"] == "0640"
    assert call.kwargs["owner"] == "root:root"


@pytest.mark.asyncio
async def test_run_script_sync_formats_exit_and_log_metadata(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].run_script.return_value = ScriptRunResult(
        result=CommandResult(stdout="ok", stderr="", exit_code=0),
        script_path="/tmp/ssh-fleet-script-abc.sh",
        runner_path="/tmp/ssh-fleet-runner-abc.sh",
        log_path="/tmp/run.log",
        kept=False,
    )

    result = await server.run_script(
        "web-1",
        "echo ok",
        sudo=True,
        log_path="/tmp/run.log",
    )

    assert "[exit_code: 0]" in result
    assert "ok" in result
    assert "log_path: /tmp/run.log" in result
    assert "script_path:" not in result
    server_env["pool"].run_script.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_script_tmux_success_returns_followup_commands(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].run_script.return_value = ScriptRunResult(
        result=CommandResult(stdout="", stderr="", exit_code=0),
        script_path="/tmp/ssh-fleet-script-abc.sh",
        runner_path="/tmp/ssh-fleet-runner-abc.sh",
        log_path="/tmp/wizinst.log",
        tmux_session="wizinst",
        kept=False,
    )

    result = await server.run_script(
        "web-1",
        "echo ok",
        tmux_session="wizinst",
    )

    assert "Launched script in tmux session 'wizinst'" in result
    assert "log_path: /tmp/wizinst.log" in result
    assert "tmux attach -t wizinst" in result
    assert "tail -f /tmp/wizinst.log" in result


@pytest.mark.asyncio
async def test_run_script_ssh_error_returns_error(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].run_script.side_effect = SSHConnectionError("connection refused")

    result = await server.run_script("web-1", "echo ok")

    assert result == "ERROR: connection refused"
