"""ssh-fleet MCP server.

Exposes SSH tools to Claude Code via the Model Context Protocol.
"""
import logging
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ssh_fleet.machines import MachineStore
from ssh_fleet.pool import ConnectionPool
from ssh_fleet.ssh import SSHConnectionError

# Configure logging to stderr (visible in Claude Code MCP debug)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ssh-fleet] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("ssh-fleet")

# Global state
store = MachineStore()
pool = ConnectionPool(idle_timeout=300)
mcp_server = FastMCP(
    "ssh-fleet",
    instructions=(
        "SSH access to a fleet of remote machines. Use for running commands"
        " (exec, sudo_exec), persistent root shells (shell_open/run/close),"
        " file operations (read_file, upload, download), and machine inventory"
        " (list_machines, add_host, reload_machines)."
        " The 'host' parameter accepts hostname OR IP address."
        " IMPORTANT: Use sudo_exec (not exec) for kubectl, systemctl, and"
        " most system commands - regular users typically lack permissions."
    ),
)

# Config paths for reload
_status_url: Optional[str] = None
_auth_file: Optional[Path] = None


def _find_config() -> Optional[Path]:
    """Find config file. Checks SSH_FLEET_CONFIG env, then default paths."""
    import os
    env_path = os.environ.get("SSH_FLEET_CONFIG")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p

    # Check default locations
    for candidate in [
        Path.home() / ".ssh-fleet" / "config.yaml",
        Path.home() / ".onwatch-debug" / "config.yaml",
    ]:
        if candidate.exists():
            return candidate
    return None


def _load_config() -> None:
    """Load config and populate machine store."""
    global _status_url, _auth_file

    try:
        import yaml
    except ImportError:
        logger.error("pyyaml not installed")
        return

    config_path = _find_config()
    if not config_path:
        logger.warning("No config found. Checked ~/.ssh-fleet/config.yaml and SSH_FLEET_CONFIG env var.")
        return

    logger.info(f"Using config: {config_path}")
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    auth_file = data.get("auth_file", "")
    if auth_file:
        _auth_file = Path(auth_file).expanduser()
        if _auth_file.exists():
            count = store.load_from_file(_auth_file)
            logger.info(f"Loaded {count} machines from {_auth_file}")
        else:
            logger.warning(f"Auth file not found: {_auth_file}")

    _status_url = data.get("status_url", "")
    if _status_url:
        store.load_dashboard(_status_url)
        logger.info("Dashboard metadata loaded")


@mcp_server.tool()
async def exec(host: str, command: str, timeout: int = 30) -> str:
    """Execute a command as the SSH user (non-root) on a remote machine.

    Runs in a login shell (bash -l) with full PATH. Supports multi-line scripts.
    Each call is independent (no state between calls).
    For repeated commands on the same machine, use shell_open + shell_run instead.

    NOTE: Most system commands (kubectl, systemctl, journalctl, crictl) require
    root. Use sudo_exec instead for those.

    Args:
        host: Hostname or IP of the target machine (case-insensitive)
        command: Shell command to execute (single or multi-line)
        timeout: Seconds before timeout (default 30)
    """
    m = store.get(host)
    if not m:
        return f"ERROR: Machine '{host}' not found. Use list_machines to see available machines."
    try:
        result = await pool.exec(m, command, timeout=timeout)
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def sudo_exec(host: str, command: str, timeout: int = 60) -> str:
    """Execute a command as root on a remote machine. Preferred for most operations.

    Runs in a login shell (bash -l) with full PATH. Uses the machine's SSH password for sudo.
    Each call is independent. For multiple commands, use shell_open + shell_run instead.

    Use this for: kubectl, systemctl, journalctl, crictl, docker/podman, file reads
    outside the SSH user's home, and any system administration commands.

    Args:
        host: Hostname or IP of the target machine (case-insensitive)
        command: Shell command to execute with sudo (single or multi-line)
        timeout: Seconds before timeout (default 60)
    """
    m = store.get(host)
    if not m:
        return f"ERROR: Machine '{host}' not found. Use list_machines to see available machines."
    try:
        result = await pool.sudo_exec(m, command, timeout=timeout)
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def list_machines() -> str:
    """List all known machines with SSH availability and dashboard metadata."""
    machines = store.list_all()
    if not machines:
        return "No machines loaded. Check ~/.ssh-fleet/config.yaml or set SSH_FLEET_CONFIG env var."

    lines = []
    for m in machines:
        status = "[ssh ready]" if m["ssh"] else "[no credentials]"
        temp = " (temporary)" if m["temporary"] else ""
        owner = f"  owner: {m['owner']}" if m.get("owner") else ""
        lines.append(
            f"{m['hostname']:<16} {m['ip']:<16} {m['version']:<14} "
            f"{m['os']:<16} {m['state']:<12} {status}{temp}{owner}"
        )
    return "\n".join(lines)


@mcp_server.tool()
async def read_file(host: str, remote_path: str, max_bytes: int = 1048576) -> str:
    """Read a remote file's contents via SFTP.

    Args:
        host: Hostname or IP of the target machine (case-insensitive)
        remote_path: Absolute path to the file on the remote machine
        max_bytes: Maximum bytes to read (default 1MB). Truncates with warning if exceeded.
    """
    m = store.get(host)
    if not m:
        return f"ERROR: Machine '{host}' not found. Use list_machines to see available machines."
    try:
        content = await pool.read_file(m, remote_path, max_bytes=max_bytes)
        return content
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except FileNotFoundError:
        return f"ERROR: File not found: {remote_path}"
    except PermissionError:
        return f"ERROR: Permission denied: {remote_path}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def upload(host: str, local_path: str, remote_path: str) -> str:
    """Upload a local file to a remote machine via SFTP.

    Args:
        host: Hostname or IP of the target machine (case-insensitive)
        local_path: Path to the file on your local machine
        remote_path: Destination path on the remote machine
    """
    import os
    m = store.get(host)
    if not m:
        return f"ERROR: Machine '{host}' not found. Use list_machines to see available machines."
    if not os.path.exists(local_path):
        return f"ERROR: Local file not found: {local_path}"
    try:
        size = await pool.upload(m, local_path, remote_path)
        return f"Uploaded {local_path} to {host}:{remote_path} ({size:,} bytes)"
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except PermissionError:
        return f"ERROR: Permission denied writing to {remote_path}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def download(host: str, remote_path: str, local_path: str) -> str:
    """Download a file from a remote machine via SFTP.

    Args:
        host: Hostname or IP of the target machine (case-insensitive)
        remote_path: Path to the file on the remote machine
        local_path: Destination path on your local machine
    """
    m = store.get(host)
    if not m:
        return f"ERROR: Machine '{host}' not found. Use list_machines to see available machines."
    try:
        size = await pool.download(m, remote_path, local_path)
        return f"Downloaded {host}:{remote_path} to {local_path} ({size:,} bytes)"
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except FileNotFoundError:
        return f"ERROR: Remote file not found: {remote_path}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def shell_open(host: str, sudo: bool = True) -> str:
    """Open a persistent interactive shell on a machine.

    Sudo defaults to true (root shell) since most operations need root.
    The shell stays open until you close it or the session ends.

    Args:
        host: Hostname or IP of the target machine (case-insensitive)
        sudo: Elevate to root via sudo (default true)
    """
    m = store.get(host)
    if not m:
        return f"ERROR: Machine '{host}' not found."
    try:
        session_id = await pool.shell_open(m, sudo=sudo)
        return f"Opened root shell on {host}. Session ID: {session_id}\nUse shell_run with this session_id to run commands."
    except SSHConnectionError as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def shell_run(session_id: str, command: str, timeout: int = 60) -> str:
    """Run a command in a persistent shell session.

    The shell is already elevated to root. State (cd, env vars) persists
    between calls. Supports multi-line commands and scripts.

    Args:
        session_id: Session ID from shell_open
        command: Command to run (single or multi-line)
        timeout: Seconds before timeout (default 60)
    """
    try:
        result = await pool.shell_run(session_id, command, timeout=timeout)
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def shell_close(session_id: str) -> str:
    """Close a persistent shell session.

    Args:
        session_id: Session ID from shell_open
    """
    await pool.shell_close(session_id)
    return f"Closed shell session {session_id}"


@mcp_server.tool()
async def shell_list() -> str:
    """List active shell sessions with machine name and idle time."""
    sessions = pool.shell_list()
    if not sessions:
        return "No active shell sessions."
    lines = []
    for s in sessions:
        lines.append(
            f"{s['session_id']:<30} {s['machine']:<16} "
            f"age: {s['created']}s  idle: {s['idle']}s"
        )
    return "\n".join(lines)


@mcp_server.tool()
async def add_host(hostname: str, ip: str, username: str,
                   password: str = "", key_file: str = "",
                   permanent: bool = False) -> str:
    """Add a machine to the fleet. Permanent saves to the hosts file for future sessions.

    The machine is available immediately for exec/sudo_exec.
    With permanent=False (default), it's session-only and gone when the session ends.
    With permanent=True, it's appended to the hosts file and persists across sessions.

    Args:
        hostname: Name for this machine
        ip: IP address
        username: SSH username
        password: SSH password (required for permanent; optional if using key_file for temp)
        key_file: Path to SSH private key (only for temporary machines)
        permanent: Save to hosts file for future sessions (default false)
    """
    try:
        if permanent:
            m = store.add_permanent(
                hostname, ip, username, password=password, auth_file=_auth_file
            )
            return f"Added '{hostname}' ({ip}) permanently to {_auth_file}. Ready for exec/sudo_exec."
        else:
            store.add_temporary(hostname, ip, username, password=password, key_file=key_file)
            auth_method = "key" if key_file else "password"
            return f"Added temporary machine '{hostname}' ({ip}, {auth_method} auth). Ready for exec/sudo_exec."
    except ValueError as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def reload_machines() -> str:
    """Re-read hosts file and refresh dashboard metadata.

    Temporary machines are preserved.
    """
    count = 0
    if _auth_file and _auth_file.exists():
        count = store.load_from_file(_auth_file)

    if _status_url:
        store.load_dashboard(_status_url)

    return f"Reloaded {count} machines from hosts file. Dashboard metadata refreshed."


def main():
    """Entry point for the ssh-fleet MCP server."""
    _load_config()
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
