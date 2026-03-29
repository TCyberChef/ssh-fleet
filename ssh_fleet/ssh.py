"""SSH connection and command execution."""
import base64
import json as _json
import random
import re
import shlex
import socket
import time
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

    def format(self, max_output: int = 50000) -> str:
        """Format for MCP tool output.

        Auto-detects and pretty-prints JSON in stdout for LLM readability.
        Truncates output exceeding max_output characters.
        """
        if self.error:
            return f"ERROR: {self.error}"
        parts = [f"[exit_code: {self.exit_code}]"]
        if self.stdout:
            formatted = _format_output(self.stdout, max_output)
            parts.append(formatted)
        if self.stderr:
            parts.append(f"STDERR: {self.stderr}")
        if self.exit_code != 0 and not self.stdout and not self.stderr:
            parts.append("(no output)")
        return "\n".join(parts)


def _format_output(text: str, max_chars: int = 50000) -> str:
    """Format command output for LLM consumption.

    Auto-detects and pretty-prints JSON (whole output or per-line).
    Truncates output exceeding max_chars.
    """
    stripped = text.strip()

    # Try whole output as JSON first
    if stripped.startswith('{') or stripped.startswith('['):
        try:
            parsed = _json.loads(stripped)
            text = _json.dumps(parsed, indent=2, ensure_ascii=False)
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n[truncated at {max_chars} chars]"
            return text
        except (ValueError, _json.JSONDecodeError):
            pass

    # Per-line: pretty-print lines that look like complete JSON objects/arrays
    lines = text.split('\n')
    result = []
    for line in lines:
        s = line.strip()
        if (len(s) > 80
                and ((s.startswith('{') and s.endswith('}'))
                     or (s.startswith('[') and s.endswith(']')))):
            try:
                parsed = _json.loads(s)
                result.append(_json.dumps(parsed, indent=2, ensure_ascii=False))
                continue
            except (ValueError, _json.JSONDecodeError):
                pass
        result.append(line)

    text = '\n'.join(result)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated at {max_chars} chars]"
    return text


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

    def __init__(self, ip: str, username: str, password: str = "",
                 key_file: str = "", timeout: int = 10):
        self.ip = ip
        self.username = username
        self.password = password
        self.key_file = key_file
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
        """Open SSH connection with keepalives enabled."""
        self.close()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs = {
                "hostname": self.ip,
                "username": self.username,
                "timeout": self.timeout,
                "banner_timeout": self.timeout,
            }
            if self.key_file:
                connect_kwargs["key_filename"] = self.key_file
                connect_kwargs["allow_agent"] = False
                connect_kwargs["look_for_keys"] = False
            elif self.password:
                connect_kwargs["password"] = self.password
                connect_kwargs["allow_agent"] = False
                connect_kwargs["look_for_keys"] = False
            else:
                # Fall back to SSH agent / default keys
                connect_kwargs["allow_agent"] = True
                connect_kwargs["look_for_keys"] = True

            client.connect(**connect_kwargs)
            # Enable SSH keepalives - detects dead connections within ~60s
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)
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
        """Execute a command in a login shell for full PATH."""
        if not self.is_connected:
            raise SSHConnectionError(f"Not connected to {self.ip}")
        try:
            full_cmd = f"bash -l -c {shlex.quote(command)}"
            stdin, stdout, stderr = self._client.exec_command(
                full_cmd, timeout=timeout, get_pty=False
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
        """Execute a command with sudo in a login shell."""
        if not self.is_connected:
            raise SSHConnectionError(f"Not connected to {self.ip}")
        escaped_pw = escape_for_shell(self.password)
        cmd_quoted = shlex.quote(command)
        sudo_cmd = f"echo {escaped_pw} | sudo -Si bash -l -c {cmd_quoted}"
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


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from terminal output."""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[[\?]?[0-9;]*[a-zA-Z]', '', text)


class SudoShell:
    """Persistent root shell via invoke_shell() + sudo su -.

    Opens an interactive shell, elevates to root once, then runs
    multiple commands without re-authenticating. Uses marker-based
    output parsing to extract command output and exit codes.
    """

    _MARKER = "___SUDOSHELL_DONE_{}_{}___"
    _START = "___SUDOSHELL_START_{}_{}___"

    def __init__(self, client: paramiko.SSHClient, password: str, timeout: int = 30):
        self.client = client
        self.password = password
        self.timeout = timeout
        self.channel = None
        self._elevated = False

    def open(self) -> None:
        """Open interactive shell and elevate to root."""
        self.channel = self.client.invoke_shell(width=200, height=50)
        self.channel.settimeout(self.timeout)

        # Wait for initial prompt
        self._read_until_prompt(timeout=10)

        # Send sudo su -
        self.channel.send("sudo su -\n")
        time.sleep(0.15)

        response = self._read_until_prompt(timeout=10)
        response_clean = _strip_ansi(response).lower()

        if 'password' in response_clean:
            self.channel.send(self.password + "\n")
            time.sleep(0.15)
            response = self._read_until_prompt(timeout=10)
            response_clean = _strip_ansi(response).lower()

            if 'sorry' in response_clean or 'incorrect' in response_clean:
                self.close()
                raise SSHConnectionError("sudo authentication failed - wrong password")

        self._elevated = True
        # Prevent marker-wrapped commands from polluting root's bash history
        self.channel.send("unset HISTFILE\n")
        time.sleep(0.1)
        self._read_until_prompt(timeout=5)

    def close(self) -> None:
        """Exit root shell and close channel."""
        if self.channel:
            try:
                if self._elevated:
                    self.channel.send("exit\n")
                    time.sleep(0.3)
                self.channel.close()
            except Exception:
                pass
            self.channel = None
            self._elevated = False

    def run(self, command: str, timeout: int = None) -> CommandResult:
        """Execute a command in the persistent root shell.

        Multi-line commands are base64-encoded to avoid interactive shell
        quoting issues. Output is parsed using start/end markers for
        reliable separation from echoed input.
        """
        if not self.channel or not self._elevated:
            raise SSHConnectionError("SudoShell not open")

        timeout = timeout or self.timeout
        rand_id = random.randint(10000, 99999)
        ts = int(time.time())
        marker = self._MARKER.format(rand_id, ts)
        start_marker = self._START.format(rand_id, ts)

        # For multi-line commands, base64 encode to avoid quoting issues
        if '\n' in command:
            encoded = base64.b64encode(command.encode()).decode()
            shell_cmd = f'echo {encoded} | base64 -d | bash'
        else:
            shell_cmd = command

        full_cmd = f'echo {start_marker}; {shell_cmd}; echo "{marker}:$?"\n'
        self.channel.send(full_cmd)

        raw = self._read_until(marker, timeout=timeout)

        # Parse output using start/end markers
        clean = _strip_ansi(raw).replace('\r\n', '\n').replace('\r', '')

        exit_code = -1
        output_lines = []
        started = False
        for line in clean.split('\n'):
            stripped = line.strip()
            if not started:
                if stripped == start_marker:
                    started = True
                continue
            if stripped.startswith(marker):
                parts = stripped.split(marker + ':')
                if len(parts) > 1:
                    try:
                        exit_code = int(parts[1].strip())
                    except ValueError:
                        exit_code = -1
                break
            output_lines.append(line)

        while output_lines and not output_lines[-1].strip():
            output_lines.pop()

        stdout = '\n'.join(output_lines).strip()
        return CommandResult(stdout=stdout, stderr='', exit_code=exit_code)

    def _read_until(self, marker: str, timeout: int = None) -> str:
        """Read until marker:N (where N is a digit) appears."""
        timeout = timeout or self.timeout
        self.channel.settimeout(timeout)
        buf = ""
        resolved_pattern = marker + ':'
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.channel.recv_ready():
                    chunk = self.channel.recv(4096).decode('utf-8', errors='replace')
                    buf += chunk
                    idx = buf.find(resolved_pattern)
                    while idx != -1:
                        after = buf[idx + len(resolved_pattern):]
                        if after and after[0].isdigit():
                            return buf
                        idx = buf.find(resolved_pattern, idx + 1)
                else:
                    time.sleep(0.05)
            except socket.timeout:
                break
        return buf

    def _read_until_prompt(self, timeout: int = None) -> str:
        """Read until shell prompt (# or $)."""
        timeout = timeout or self.timeout
        self.channel.settimeout(timeout)
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.channel.recv_ready():
                    chunk = self.channel.recv(4096).decode('utf-8', errors='replace')
                    buf += chunk
                    stripped = _strip_ansi(buf).rstrip()
                    if stripped.endswith('#') or stripped.endswith('$'):
                        return buf
                else:
                    time.sleep(0.05)
            except socket.timeout:
                break
        return buf
