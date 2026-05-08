"""Tests for connection pool."""
import asyncio
import time
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch
from ssh_fleet.pool import ConnectionPool
from ssh_fleet.machines import Machine
from ssh_fleet.ssh import CommandResult, ScriptRunResult


@pytest.fixture
def machine():
    return Machine(hostname="web-1", ip="192.168.1.10", username="admin", password="pass")


@pytest_asyncio.fixture
async def pool():
    p = ConnectionPool(idle_timeout=2)  # 2s for testing
    yield p
    await p.close_all()


@pytest.mark.asyncio
async def test_pool_creates_connection(pool, machine):
    with patch("ssh_fleet.pool.SSHConnection") as MockConn:
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.run.return_value = MagicMock(stdout="hello", stderr="", exit_code=0, error=None)
        MockConn.return_value = mock_conn

        result = await pool.exec(machine, "echo hello")
        assert result.stdout == "hello"
        mock_conn.connect.assert_called_once()


@pytest.mark.asyncio
async def test_pool_reuses_connection(pool, machine):
    with patch("ssh_fleet.pool.SSHConnection") as MockConn:
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.run.return_value = MagicMock(stdout="ok", stderr="", exit_code=0, error=None)
        MockConn.return_value = mock_conn

        await pool.exec(machine, "echo 1")
        await pool.exec(machine, "echo 2")
        # Should only connect once
        assert MockConn.call_count == 1


@pytest.mark.asyncio
async def test_pool_reconnects_on_dead_connection(pool, machine):
    with patch("ssh_fleet.pool.SSHConnection") as MockConn:
        mock_conn = MagicMock()
        mock_conn.run.return_value = MagicMock(stdout="ok", stderr="", exit_code=0, error=None)
        MockConn.return_value = mock_conn

        # First call - connection is alive
        mock_conn.is_connected = True
        await pool.exec(machine, "echo 1")
        assert mock_conn.connect.call_count == 1

        # Simulate connection dying
        mock_conn.is_connected = False
        await pool.exec(machine, "echo 2")
        # Should have reconnected
        assert mock_conn.connect.call_count == 2


def test_pool_get_lock_same_machine():
    pool = ConnectionPool()
    lock1 = pool._get_lock("ferrari")
    lock2 = pool._get_lock("ferrari")
    assert lock1 is lock2


def test_pool_get_lock_different_machines():
    pool = ConnectionPool()
    lock1 = pool._get_lock("ferrari")
    lock2 = pool._get_lock("azul")
    assert lock1 is not lock2


@pytest.mark.asyncio
async def test_pool_write_file_delegates_to_connection(pool, machine):
    with patch("ssh_fleet.pool.SSHConnection") as MockConn:
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.write_file.return_value = 5
        MockConn.return_value = mock_conn

        result = await pool.write_file(
            machine,
            "/etc/example.conf",
            "hello",
            sudo=True,
            mode="0640",
            owner="root:root",
            timeout=90,
        )

        assert result == 5
        mock_conn.write_file.assert_called_once_with(
            "/etc/example.conf",
            "hello",
            True,
            "0640",
            "root:root",
            90,
        )


@pytest.mark.asyncio
async def test_pool_run_script_delegates_to_connection(pool, machine):
    with patch("ssh_fleet.pool.SSHConnection") as MockConn:
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        script_result = ScriptRunResult(
            result=CommandResult(stdout="ok", stderr="", exit_code=0),
            script_path="/tmp/script",
            runner_path="/tmp/runner",
        )
        mock_conn.run_script.return_value = script_result
        MockConn.return_value = mock_conn

        result = await pool.run_script(
            machine,
            "echo ok",
            sudo=True,
            timeout=90,
            tmux_session="wizinst",
            log_path="/tmp/run.log",
            env={"A": "B"},
            keep_script=True,
        )

        assert result is script_result
        mock_conn.run_script.assert_called_once_with(
            "echo ok",
            True,
            90,
            "wizinst",
            "/tmp/run.log",
            {"A": "B"},
            True,
        )


@pytest.mark.asyncio
async def test_pool_tmux_paste_delegates_to_connection(pool, machine):
    with patch("ssh_fleet.pool.SSHConnection") as MockConn:
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.tmux_paste.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        MockConn.return_value = mock_conn

        result = await pool.tmux_paste(
            machine,
            "work_1",
            "echo ok\n",
            enter=True,
            sudo=True,
            timeout=25,
        )

        assert result.exit_code == 0
        mock_conn.tmux_paste.assert_called_once_with(
            "work_1",
            "echo ok\n",
            True,
            True,
            25,
        )


@pytest.mark.asyncio
async def test_pool_tmux_wait_delegates_to_connection(pool, machine):
    with patch("ssh_fleet.pool.SSHConnection") as MockConn:
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.tmux_wait.return_value = CommandResult(stdout="MATCHED: READY", stderr="", exit_code=0)
        MockConn.return_value = mock_conn

        result = await pool.tmux_wait(
            machine,
            "work_1",
            "READY",
            regex=False,
            timeout=30,
            interval=0.5,
            lines=100,
            sudo=True,
        )

        assert result.exit_code == 0
        mock_conn.tmux_wait.assert_called_once_with(
            "work_1",
            "READY",
            False,
            30,
            0.5,
            100,
            True,
        )
