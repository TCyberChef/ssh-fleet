"""SSH connection and command execution.

Adapted from onwatch-debug/scripts/ssh_utils.py.
"""
import re
import socket
from dataclasses import dataclass
from typing import Optional

import paramiko
from paramiko.ssh_exception import SSHException, AuthenticationException


@dataclass
class CommandResult:
    """Result of an SSH command execution."""
    stdout: str
    stderr: str
    exit_code: int
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Return stdout, or stderr if stdout is empty."""
        return self.stdout or self.stderr

    def format(self) -> str:
        """Format for MCP tool output."""
        if self.error:
            return f"ERROR: {self.error}"
        parts = [f"[exit_code: {self.exit_code}]"]
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"STDERR: {self.stderr}")
        return "\n".join(parts)


def escape_for_shell(value: str) -> str:
    """Escape a string for safe use in shell single quotes."""
    escaped = value.replace("'", "'\\''")
    return f"'{escaped}'"


def strip_sudo_prompt(output: str) -> str:
    """Strip [sudo] password prompt from output."""
    lines = output.split("\n")
    cleaned = []
    for line in lines:
        if line.startswith("[sudo] password for "):
            after = line.split(": ", 1)
            if len(after) > 1 and after[1].strip():
                cleaned.append(after[1])
        else:
            cleaned.append(line)
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    return "\n".join(cleaned)


class SSHConnectionError(Exception):
    """SSH connection failed."""
    pass


class SSHAuthError(SSHConnectionError):
    """Authentication or host resolution failed. Do not retry."""
    pass


class SSHConnection:
    """Persistent SSH connection to a single machine.

    Wraps paramiko.SSHClient with connect/close/run/run_sudo/sftp.
    Designed to be held in a connection pool.
    """

    def __init__(self, ip: str, username: str, password: str, timeout: int = 10):
        self.ip = ip
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

    @property
    def is_connected(self) -> bool:
        """Check if the SSH connection is still alive."""
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def connect(self) -> None:
        """Open SSH connection."""
        self.close()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.ip,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            self._client = client
        except AuthenticationException:
            raise SSHAuthError(f"Authentication failed for {self.username}@{self.ip}")
        except socket.timeout:
            raise SSHConnectionError(f"Connection timed out: {self.ip}")
        except socket.gaierror:
            raise SSHAuthError(f"Cannot resolve hostname: {self.ip}")
        except SSHException as e:
            raise SSHConnectionError(f"SSH error connecting to {self.ip}: {e}")
        except OSError as e:
            raise SSHAuthError(f"Cannot reach {self.ip}: {e}")

    @property
    def sftp(self) -> paramiko.SFTPClient:
        """Get or create SFTP client (lazy)."""
        if not self.is_connected:
            raise SSHConnectionError(f"Not connected to {self.ip}")
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def close(self) -> None:
        """Close SSH connection and SFTP."""
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def run(self, command: str, timeout: int = 30) -> CommandResult:
        """Execute a command."""
        if not self.is_connected:
            raise SSHConnectionError(f"Not connected to {self.ip}")
        try:
            stdin, stdout, stderr = self._client.exec_command(
                command, timeout=timeout, get_pty=False
            )
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
            return CommandResult(stdout=out, stderr=err, exit_code=exit_code)
        except socket.timeout:
            return CommandResult(stdout="", stderr="", exit_code=-1, error="Command timed out")
        except SSHException as e:
            return CommandResult(stdout="", stderr="", exit_code=-1, error=f"SSH error: {e}")

    def run_sudo(self, command: str, timeout: int = 60) -> CommandResult:
        """Execute a command with sudo via echo-pipe method."""
        if not self.is_connected:
            raise SSHConnectionError(f"Not connected to {self.ip}")
        escaped_pw = escape_for_shell(self.password)
        sudo_cmd = f"echo {escaped_pw} | sudo -Si {command}"
        try:
            stdin, stdout, stderr = self._client.exec_command(
                sudo_cmd, timeout=timeout, get_pty=True
            )
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
            out = strip_sudo_prompt(out)
            err = strip_sudo_prompt(err)
            return CommandResult(stdout=out, stderr=err, exit_code=exit_code)
        except socket.timeout:
            return CommandResult(stdout="", stderr="", exit_code=-1, error="Command timed out")
        except SSHException as e:
            return CommandResult(stdout="", stderr="", exit_code=-1, error=f"SSH error: {e}")

    def read_remote_file(self, remote_path: str, max_bytes: int = 1048576) -> str:
        """Read a remote file via SFTP. Returns content as text."""
        with self.sftp.open(remote_path, "rb") as f:
            chunk = f.read(1024)
            if b"\x00" in chunk:
                raise SSHConnectionError(f"Binary file detected: {remote_path}")
            rest = f.read(max_bytes - len(chunk))
            data = chunk + rest
        text = data.decode("utf-8", errors="replace")
        if len(data) >= max_bytes:
            text += f"\n\n[TRUNCATED at {max_bytes} bytes]"
        return text

    def upload_file(self, local_path: str, remote_path: str) -> int:
        """Upload a local file to remote via SFTP. Returns bytes transferred."""
        import os
        self.sftp.put(local_path, remote_path)
        return os.path.getsize(local_path)

    def download_file(self, remote_path: str, local_path: str) -> int:
        """Download a remote file to local via SFTP. Returns bytes transferred."""
        import os
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        self.sftp.get(remote_path, local_path)
        return os.path.getsize(local_path)
