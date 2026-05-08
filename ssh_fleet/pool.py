"""Connection pool with idle cleanup and per-machine locking."""
import asyncio
import logging
import time
from typing import Optional

from ssh_fleet.ssh import (
    SSHConnection, SSHConnectionError, SSHAuthError, CommandResult,
    ScriptRunResult, SudoShell,
)
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
        self._shells: dict[str, dict] = {}  # session_id -> {machine, shell, created, last_used}
        self._idle_timeout = idle_timeout
        self._shell_idle_timeout = 1800  # 30 minutes
        self._cleanup_task: Optional[asyncio.Task] = None
        self._session_counter = 0
        self._cleanup_started = False

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
            key_file=machine.key_file,
        )
        conn.connect()
        self._pool[key] = PoolEntry(conn)
        logger.info(f"Connected to {machine.hostname} ({machine.ip})")
        return conn

    def _ensure_cleanup(self):
        """Start the background cleanup loop if not already running."""
        if not self._cleanup_started:
            self._cleanup_started = True
            try:
                asyncio.get_running_loop()
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            except RuntimeError:
                pass  # No event loop yet, skip

    async def _cleanup_loop(self):
        """Background loop that cleans up idle connections and shells."""
        while True:
            await asyncio.sleep(60)
            self.cleanup_idle()

    async def exec(self, machine: Machine, command: str, timeout: int = 30) -> CommandResult:
        """Execute a command on a machine, using pooled connection."""
        self._ensure_cleanup()
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

    async def write_file(
        self,
        machine: Machine,
        remote_path: str,
        content: str,
        sudo: bool = False,
        mode: str = "0644",
        owner: Optional[str] = None,
        timeout: int = 60,
    ) -> int:
        """Write text content to a remote file via SFTP staging."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(
                conn.write_file,
                remote_path,
                content,
                sudo,
                mode,
                owner,
                timeout,
            )

    async def run_script(
        self,
        machine: Machine,
        script: str,
        sudo: bool = True,
        timeout: int = 60,
        tmux_session: Optional[str] = None,
        log_path: Optional[str] = None,
        env: Optional[dict[str, object]] = None,
        keep_script: bool = False,
    ) -> ScriptRunResult:
        """Stage and run a remote bash script through SFTP-backed files."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(
                conn.run_script,
                script,
                sudo,
                timeout,
                tmux_session,
                log_path,
                env,
                keep_script,
            )

    async def tmux_new(
        self,
        machine: Machine,
        session: str,
        command: str = "",
        cwd: str = "",
        sudo: bool = True,
        timeout: int = 30,
    ) -> CommandResult:
        """Create a detached tmux session on a machine."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(
                conn.tmux_new,
                session,
                command,
                cwd,
                sudo,
                timeout,
            )

    async def tmux_list(
        self,
        machine: Machine,
        sudo: bool = True,
        timeout: int = 20,
    ) -> CommandResult:
        """List tmux sessions on a machine."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(conn.tmux_list, sudo, timeout)

    async def tmux_capture(
        self,
        machine: Machine,
        target: str,
        lines: int = 200,
        sudo: bool = True,
        timeout: int = 20,
    ) -> CommandResult:
        """Capture tmux pane output on a machine."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(
                conn.tmux_capture,
                target,
                lines,
                sudo,
                timeout,
            )

    async def tmux_wait(
        self,
        machine: Machine,
        target: str,
        pattern: str,
        regex: bool = False,
        timeout: int = 60,
        interval: float = 1.0,
        lines: int = 200,
        sudo: bool = True,
    ) -> CommandResult:
        """Wait for a string or regex to appear in a tmux pane."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(
                conn.tmux_wait,
                target,
                pattern,
                regex,
                timeout,
                interval,
                lines,
                sudo,
            )

    async def tmux_paste(
        self,
        machine: Machine,
        target: str,
        text: str,
        enter: bool = False,
        sudo: bool = True,
        timeout: int = 20,
    ) -> CommandResult:
        """Paste arbitrary text into a remote tmux pane."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(
                conn.tmux_paste,
                target,
                text,
                enter,
                sudo,
                timeout,
            )

    async def tmux_send_keys(
        self,
        machine: Machine,
        target: str,
        keys: str,
        sudo: bool = True,
        timeout: int = 20,
    ) -> CommandResult:
        """Send tmux key tokens to a remote pane."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(
                conn.tmux_send_keys,
                target,
                keys,
                sudo,
                timeout,
            )

    async def tmux_kill(
        self,
        machine: Machine,
        target: str,
        sudo: bool = True,
        timeout: int = 20,
    ) -> CommandResult:
        """Kill a tmux session on a machine."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            return await asyncio.to_thread(
                conn.tmux_kill,
                target,
                sudo,
                timeout,
            )

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

    async def shell_open(self, machine: Machine, sudo: bool = True) -> str:
        """Open a persistent shell session. Returns session ID."""
        key = machine.hostname.lower()
        lock = self._get_lock(key)
        async with lock:
            conn = await asyncio.to_thread(self._get_or_create, machine)
            if sudo:
                shell = SudoShell(conn._client, machine.password)
                await asyncio.to_thread(shell.open)
            else:
                # Non-sudo shell not implemented yet
                raise SSHConnectionError("Non-sudo shells not yet supported")

            self._session_counter += 1
            session_id = f"shell-{machine.hostname.lower()}-{self._session_counter}"
            self._shells[session_id] = {
                "machine": machine.hostname,
                "shell": shell,
                "created": time.time(),
                "last_used": time.time(),
            }
            logger.info(f"Opened shell session {session_id} on {machine.hostname}")
            return session_id

    async def shell_run(self, session_id: str, command: str, timeout: int = 60) -> CommandResult:
        """Run a command in a persistent shell session."""
        entry = self._shells.get(session_id)
        if not entry:
            raise SSHConnectionError(f"Shell session '{session_id}' not found. Use shell_list to see active sessions.")
        entry["last_used"] = time.time()
        try:
            return await asyncio.to_thread(entry["shell"].run, command, timeout)
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["socket is closed", "not open", "eof", "channel closed"]):
                machine = entry["machine"]
                self._shells.pop(session_id, None)
                try:
                    entry["shell"].close()
                except Exception:
                    pass
                raise SSHConnectionError(
                    f"Shell '{session_id}' died (connection lost). "
                    f"Use shell_open on {machine} to start a new session."
                )
            raise

    async def shell_close(self, session_id: str) -> None:
        """Close a persistent shell session."""
        entry = self._shells.pop(session_id, None)
        if entry:
            await asyncio.to_thread(entry["shell"].close)
            logger.info(f"Closed shell session {session_id}")

    def shell_list(self) -> list[dict]:
        """List active shell sessions."""
        now = time.time()
        result = []
        for sid, entry in self._shells.items():
            result.append({
                "session_id": sid,
                "machine": entry["machine"],
                "created": int(now - entry["created"]),
                "idle": int(now - entry["last_used"]),
            })
        return result

    def cleanup_idle(self) -> int:
        """Close connections and shells idle longer than timeout. Returns count closed."""
        now = time.time()
        closed = 0

        # Cleanup idle connections
        to_remove = []
        for key, entry in self._pool.items():
            if now - entry.last_used > self._idle_timeout:
                entry.conn.close()
                to_remove.append(key)
                closed += 1
        for key in to_remove:
            del self._pool[key]
            logger.info(f"Closed idle connection: {key}")

        # Cleanup idle shells
        shell_remove = []
        for sid, entry in self._shells.items():
            if now - entry["last_used"] > self._shell_idle_timeout:
                entry["shell"].close()
                shell_remove.append(sid)
                closed += 1
        for sid in shell_remove:
            del self._shells[sid]
            logger.info(f"Closed idle shell: {sid}")

        return closed

    async def start_cleanup_loop(self):
        """Start background idle cleanup task. Called automatically on first use."""
        self._ensure_cleanup()

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
