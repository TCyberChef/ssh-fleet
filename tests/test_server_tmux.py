"""Tests for MCP-facing tmux control tools."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from ssh_fleet import server
from ssh_fleet.machines import MachineStore
from ssh_fleet.ssh import CommandResult, SSHConnectionError


@pytest.fixture
def server_env(monkeypatch):
    store = MachineStore()
    pool = MagicMock()
    pool.tmux_new = AsyncMock()
    pool.tmux_list = AsyncMock()
    pool.tmux_capture = AsyncMock()
    pool.tmux_wait = AsyncMock()
    pool.tmux_paste = AsyncMock()
    pool.tmux_send_keys = AsyncMock()
    pool.tmux_kill = AsyncMock()
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "pool", pool)
    return {"store": store, "pool": pool}


@pytest.mark.asyncio
async def test_tmux_new_unknown_host_returns_error(server_env):
    result = await server.tmux_new("ghost", "work_1")

    assert result == (
        "ERROR: Machine 'ghost' not found."
        " Use list_machines to see available machines."
    )
    server_env["pool"].tmux_new.assert_not_called()


@pytest.mark.asyncio
async def test_tmux_new_formats_result(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].tmux_new.return_value = CommandResult(stdout="", stderr="", exit_code=0)

    result = await server.tmux_new("web-1", "work_1", command="bash", cwd="/opt/onwatch")

    assert result == "Started tmux session 'work_1' on web-1."
    server_env["pool"].tmux_new.assert_awaited_once()
    call = server_env["pool"].tmux_new.await_args
    assert call.args[1] == "work_1"
    assert call.kwargs["command"] == "bash"
    assert call.kwargs["cwd"] == "/opt/onwatch"


@pytest.mark.asyncio
async def test_tmux_capture_returns_command_output(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].tmux_capture.return_value = CommandResult(
        stdout="remote output", stderr="", exit_code=0
    )

    result = await server.tmux_capture("web-1", "work_1", lines=50)

    assert "[exit_code: 0]" in result
    assert "remote output" in result


@pytest.mark.asyncio
async def test_tmux_paste_success_reports_character_count(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].tmux_paste.return_value = CommandResult(stdout="", stderr="", exit_code=0)

    result = await server.tmux_paste("web-1", "work_1", "echo ok\n", enter=True)

    assert result == "Pasted 8 characters to tmux target 'work_1' on web-1."
    server_env["pool"].tmux_paste.assert_awaited_once()
    call = server_env["pool"].tmux_paste.await_args
    assert call.args[1] == "work_1"
    assert call.args[2] == "echo ok\n"
    assert call.kwargs["enter"] is True


@pytest.mark.asyncio
async def test_tmux_wait_returns_command_output(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].tmux_wait.return_value = CommandResult(
        stdout="MATCHED: READY\nREADY", stderr="", exit_code=0
    )

    result = await server.tmux_wait("web-1", "work_1", "READY", timeout=30, interval=0.5)

    assert "[exit_code: 0]" in result
    assert "MATCHED: READY" in result
    server_env["pool"].tmux_wait.assert_awaited_once()
    call = server_env["pool"].tmux_wait.await_args
    assert call.args[1] == "work_1"
    assert call.args[2] == "READY"
    assert call.kwargs["timeout"] == 30
    assert call.kwargs["interval"] == 0.5


@pytest.mark.asyncio
async def test_tmux_send_keys_success_reports_keys(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].tmux_send_keys.return_value = CommandResult(stdout="", stderr="", exit_code=0)

    result = await server.tmux_send_keys("web-1", "work_1", "C-c")

    assert result == "Sent keys to tmux target 'work_1' on web-1: C-c"


@pytest.mark.asyncio
async def test_tmux_kill_ssh_error_returns_error(server_env):
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].tmux_kill.side_effect = SSHConnectionError("connection refused")

    result = await server.tmux_kill("web-1", "work_1")

    assert result == "ERROR: connection refused"
