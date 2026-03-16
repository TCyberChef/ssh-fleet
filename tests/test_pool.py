"""Tests for connection pool."""
import asyncio
import time
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch
from ssh_fleet.pool import ConnectionPool
from ssh_fleet.machines import Machine


@pytest.fixture
def machine():
    return Machine(hostname="Ferrari", ip="10.1.25.5", username="user", password="pass")


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
