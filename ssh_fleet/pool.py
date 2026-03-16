"""Connection pool with idle cleanup and per-machine locking."""
import asyncio
import logging
import time
from typing import Optional

from ssh_fleet.ssh import SSHConnection, SSHConnectionError, SSHAuthError, CommandResult
from ssh_fleet.machines import Machine

logger = logging.getLogger("ssh-fleet")


class PoolEntry:
    """A pooled SSH connection."""
    __slots__ = ("conn", "last_used")

    def __init__(self, conn: SSHConnection):
        self.conn = conn
        self.last_used = time.time()

    def touch(self):
        self.last_used = time.time()


class ConnectionPool:
    """Manages pooled SSH connections with idle timeout.

    - One connection per machine, reused across tool calls
    - asyncio.Lock per machine prevents concurrent access
    - Idle connections cleaned up after idle_timeout seconds
    - Dead connections reconnected transparently
    """

    def __init__(self, idle_timeout: int = 300):
        self._pool: dict[str, PoolEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._idle_timeout = idle_timeout
        self._cleanup_task: Optional[asyncio.Task] = None

    def _get_lock(self, key: str) -> asyncio.Lock:
        """Get or create a per-machine lock."""
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _get_or_create(self, machine: Machine) -> SSHConnection:
        """Get existing connection or create new one. NOT thread-safe - caller must hold lock."""
        key = machine.hostname.lower()
        entry = self._pool.get(key)

        if entry and entry.conn.is_connected:
            entry.touch()
            return entry.conn

        # Need new connection
        if entry:
            entry.conn.close()

        conn = SSHConnection(
            ip=machine.ip,
            username=machine.username,
            password=machine.password,
        )
        conn.connect()
        self._pool[key] = PoolEntry(conn)
        logger.info(f"Connected to {machine.hostname} ({machine.ip})")
        return conn

    async def exec(self, machine: Machine, command: str, timeout: int = 30) -> CommandResult:
        """Execute a command on a machine, using pooled connection."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)

        async with lock:
            try:
                conn = await asyncio.to_thread(self._get_or_create, machine)
                result = await asyncio.to_thread(conn.run, command, timeout)
                return result
            except SSHConnectionError as e:
                # No retry on auth/host errors, one retry on transient errors
                if isinstance(e, SSHAuthError):
                    raise
                try:
                    self._pool.pop(key, None)
                    conn = await asyncio.to_thread(self._get_or_create, machine)
                    result = await asyncio.to_thread(conn.run, command, timeout)
                    return result
                except SSHConnectionError:
                    raise

    async def sudo_exec(self, machine: Machine, command: str, timeout: int = 60) -> CommandResult:
        """Execute a sudo command on a machine, using pooled connection."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)

        async with lock:
            try:
                conn = await asyncio.to_thread(self._get_or_create, machine)
                result = await asyncio.to_thread(conn.run_sudo, command, timeout)
                return result
            except SSHConnectionError as e:
                # No retry on auth/host errors, one retry on transient errors
                if isinstance(e, SSHAuthError):
                    raise
                try:
                    self._pool.pop(key, None)
                    conn = await asyncio.to_thread(self._get_or_create, machine)
                    result = await asyncio.to_thread(conn.run_sudo, command, timeout)
                    return result
                except SSHConnectionError:
                    raise

    async def read_file(self, machine: Machine, remote_path: str, max_bytes: int = 1048576) -> str:
        """Read a remote file via SFTP."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(conn.read_remote_file, remote_path, max_bytes)

    async def upload(self, machine: Machine, local_path: str, remote_path: str) -> int:
        """Upload a local file to remote via SFTP. Returns bytes."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(conn.upload_file, local_path, remote_path)

    async def download(self, machine: Machine, remote_path: str, local_path: str) -> int:
        """Download a remote file to local via SFTP. Returns bytes."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(conn.download_file, remote_path, local_path)

    def cleanup_idle(self) -> int:
        """Close connections idle longer than timeout. Returns count closed."""
        now = time.time()
        closed = 0
        to_remove = []
        for key, entry in self._pool.items():
            if now - entry.last_used > self._idle_timeout:
                entry.conn.close()
                to_remove.append(key)
                closed += 1
        for key in to_remove:
            del self._pool[key]
            logger.info(f"Closed idle connection: {key}")
        return closed

    async def start_cleanup_loop(self):
        """Start background idle cleanup task."""
        async def _loop():
            while True:
                await asyncio.sleep(60)
                self.cleanup_idle()  # run on event loop, not in thread - it's fast
        self._cleanup_task = asyncio.create_task(_loop())

    async def close_all(self):
        """Close all connections and stop cleanup."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        for entry in self._pool.values():
            entry.conn.close()
        self._pool.clear()
        self._locks.clear()
