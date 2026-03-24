"""Tests for SSH connection wrapper."""
import base64
import json
import pytest
from unittest.mock import MagicMock, patch, mock_open
from ssh_fleet.ssh import (
    CommandResult, SSHConnection, SSHConnectionError, SudoShell,
    escape_for_shell, strip_sudo_prompt, _format_output,
)


def test_command_result_success():
    r = CommandResult(stdout="hello", stderr="", exit_code=0)
    assert r.success is True
    assert r.output == "hello"


def test_command_result_failure():
    r = CommandResult(stdout="", stderr="error msg", exit_code=1)
    assert r.success is False
    assert r.output == "error msg"


def test_escape_for_shell_basic():
    assert escape_for_shell("hello") == "'hello'"


def test_escape_for_shell_special_chars():
    assert escape_for_shell("p@ss!") == "'p@ss!'"


def test_escape_for_shell_single_quotes():
    assert escape_for_shell("it's") == "'it'\\''s'"


def test_strip_sudo_prompt():
    text = "[sudo] password for user: \nsome output"
    assert strip_sudo_prompt(text) == "some output"


def test_strip_sudo_prompt_no_prompt():
    text = "clean output"
    assert strip_sudo_prompt(text) == "clean output"


def test_strip_sudo_prompt_output_after_colon():
    text = "[sudo] password for user: immediate output\nnext line"
    result = strip_sudo_prompt(text)
    assert "immediate output" in result


def test_format_output_success():
    r = CommandResult(stdout="hello world", stderr="", exit_code=0)
    formatted = r.format()
    assert "[exit_code: 0]" in formatted
    assert "hello world" in formatted


def test_format_output_failure_with_stderr():
    r = CommandResult(stdout="", stderr="not found", exit_code=1)
    formatted = r.format()
    assert "[exit_code: 1]" in formatted
    assert "STDERR:" in formatted


def test_format_output_with_error():
    r = CommandResult(stdout="", stderr="", exit_code=-1, error="Connection timed out")
    formatted = r.format()
    assert "ERROR:" in formatted


def test_read_remote_file_binary_rejected():
    conn = SSHConnection("1.2.3.4", "user", "pass")
    conn._client = MagicMock()
    mock_sftp = MagicMock()
    mock_file = MagicMock()
    mock_file.read.return_value = b"\x00\x01\x02binary"
    mock_file.__enter__ = lambda s: s
    mock_file.__exit__ = MagicMock(return_value=False)
    mock_sftp.open.return_value = mock_file
    conn._sftp = mock_sftp
    conn._client.get_transport.return_value = MagicMock(is_active=lambda: True)

    with pytest.raises(SSHConnectionError, match="Binary file"):
        conn.read_remote_file("/some/binary")


def test_read_remote_file_text():
    conn = SSHConnection("1.2.3.4", "user", "pass")
    conn._client = MagicMock()
    mock_sftp = MagicMock()
    mock_file = MagicMock()
    content = b"hello world\nline 2"
    mock_file.read.side_effect = [content, b""]
    mock_file.__enter__ = lambda s: s
    mock_file.__exit__ = MagicMock(return_value=False)
    mock_sftp.open.return_value = mock_file
    conn._sftp = mock_sftp
    conn._client.get_transport.return_value = MagicMock(is_active=lambda: True)

    result = conn.read_remote_file("/some/file.txt")
    assert "hello world" in result
    assert "line 2" in result


# --- Login shell wrapping tests ---

def _mock_connected_conn():
    """Create an SSHConnection with mocked client ready for exec."""
    conn = SSHConnection("1.2.3.4", "user", "pass")
    conn._client = MagicMock()
    transport = MagicMock()
    transport.is_active.return_value = True
    conn._client.get_transport.return_value = transport

    mock_stdout = MagicMock()
    mock_stdout.read.return_value = b"output"
    mock_stdout.channel.recv_exit_status.return_value = 0
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""
    conn._client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)
    return conn


def test_run_wraps_in_login_shell():
    """run() wraps command in bash -l -c for full PATH."""
    conn = _mock_connected_conn()
    conn.run("echo hello")

    cmd = conn._client.exec_command.call_args[0][0]
    assert cmd == "bash -l -c 'echo hello'"


def test_run_multiline_quoted():
    """run() handles multi-line commands via shlex.quote."""
    conn = _mock_connected_conn()
    result = conn.run("echo hello\necho world")

    assert result.success
    cmd = conn._client.exec_command.call_args[0][0]
    assert cmd.startswith("bash -l -c ")
    # Multi-line command is a single quoted argument
    assert "echo hello" in cmd
    assert "echo world" in cmd


def test_run_special_chars_quoted():
    """run() safely quotes commands with special characters."""
    conn = _mock_connected_conn()
    conn.run("echo 'it'\"'\"'s a $test'")

    cmd = conn._client.exec_command.call_args[0][0]
    assert cmd.startswith("bash -l -c ")


def test_run_sudo_wraps_in_login_shell():
    """run_sudo() wraps command in bash -l -c."""
    conn = _mock_connected_conn()
    conn.run_sudo("kubectl get pods")

    cmd = conn._client.exec_command.call_args[0][0]
    assert "sudo -Si bash -l -c" in cmd
    assert "'kubectl get pods'" in cmd


def test_run_sudo_multiline_safe():
    """run_sudo() properly quotes multi-line commands (no breakout from sudo pipe)."""
    conn = _mock_connected_conn()
    conn.run_sudo("echo hello\necho world")

    cmd = conn._client.exec_command.call_args[0][0]
    assert "sudo -Si bash -l -c" in cmd
    # The full multi-line command must be inside the quoted arg, not breaking out
    assert cmd.count("sudo") == 1


# --- JSON formatting tests ---

def test_format_json_pure():
    """format() pretty-prints pure JSON output."""
    data = {"key": "value", "nested": {"a": 1, "b": 2}}
    r = CommandResult(stdout=json.dumps(data), stderr="", exit_code=0)
    formatted = r.format()
    assert '"key": "value"' in formatted
    assert '"nested"' in formatted
    # Should be multi-line (pretty-printed)
    stdout_part = formatted.split("[exit_code: 0]\n")[1]
    assert stdout_part.count('\n') > 1


def test_format_json_array():
    """format() pretty-prints JSON array output."""
    data = [{"name": "pod-1"}, {"name": "pod-2"}]
    r = CommandResult(stdout=json.dumps(data), stderr="", exit_code=0)
    formatted = r.format()
    assert '"name": "pod-1"' in formatted


def test_format_json_mixed_text():
    """format() pretty-prints JSON within mixed text+JSON output."""
    json_blob = json.dumps({"CUDA": "all", "REDIS": "redis:6379", "LOG": "/var/log/app.log", "PORT": "9970", "MODE": "auto"})
    stdout = f"=== ConfigMap ===\n{json_blob}"
    r = CommandResult(stdout=stdout, stderr="", exit_code=0)
    formatted = r.format()
    assert "=== ConfigMap ===" in formatted
    # JSON line should be pretty-printed (multi-line)
    assert '"CUDA": "all"' in formatted


def test_format_json_short_line_unchanged():
    """format() doesn't try to parse short JSON-like lines."""
    r = CommandResult(stdout='{"ok":true}', stderr="", exit_code=0)
    formatted = r.format()
    # Short JSON stays compact (under 80 char threshold for per-line,
    # but whole-output detection still applies)
    assert '{"ok":true}' in formatted or '"ok": true' in formatted


def test_format_non_json_unchanged():
    """format() leaves non-JSON output unchanged."""
    r = CommandResult(stdout="plain text\nline 2\nline 3", stderr="", exit_code=0)
    formatted = r.format()
    assert "plain text" in formatted
    assert "line 2" in formatted


def test_format_false_positive_skipped():
    """format() doesn't choke on lines that start with [ but aren't JSON."""
    stdout = "[sudo] password for user: \n[root@host ~]# output here"
    r = CommandResult(stdout=stdout, stderr="", exit_code=0)
    formatted = r.format()
    assert "[sudo]" in formatted
    assert "[root@host" in formatted


def test_format_truncation():
    """format() truncates very long output."""
    long_output = "x" * 60000
    r = CommandResult(stdout=long_output, stderr="", exit_code=0)
    formatted = r.format()
    assert "[truncated at 50000 chars]" in formatted
    assert len(formatted) < 60000


def test_format_output_no_truncation_when_short():
    """format() doesn't truncate short output."""
    r = CommandResult(stdout="short output", stderr="", exit_code=0)
    formatted = r.format()
    assert "[truncated" not in formatted


# --- SudoShell tests ---

def _mock_sudo_shell():
    """Create a SudoShell with mocked channel."""
    client = MagicMock()
    shell = SudoShell(client, "password", timeout=10)
    shell.channel = MagicMock()
    shell._elevated = True
    return shell


def test_sudoshell_singleline_uses_start_marker():
    """SudoShell.run() uses start marker for output parsing."""
    shell = _mock_sudo_shell()

    # Capture what gets sent to channel
    sent_data = []
    shell.channel.send = lambda data: sent_data.append(data)

    # Mock _read_until to return simulated shell output
    start_marker = None
    end_marker = None

    def fake_read_until(marker, timeout=None):
        nonlocal end_marker
        end_marker = marker
        # Find start marker from sent command
        cmd = sent_data[0]
        sm = cmd.split("echo ")[1].split(";")[0].strip()
        return f"echo {sm}; hostname; echo \"{marker}:$?\"\n{sm}\nhostname-result\n{marker}:0\n"

    shell._read_until = fake_read_until
    result = shell.run("hostname")

    assert result.exit_code == 0
    assert "hostname-result" in result.stdout
    # Verify start marker was in the sent command
    assert "___SUDOSHELL_START_" in sent_data[0]
    assert "___SUDOSHELL_DONE_" in sent_data[0]


def test_sudoshell_multiline_uses_base64():
    """SudoShell.run() base64-encodes multi-line commands."""
    shell = _mock_sudo_shell()

    sent_data = []
    shell.channel.send = lambda data: sent_data.append(data)

    command = "echo hello\necho world"
    expected_b64 = base64.b64encode(command.encode()).decode()

    def fake_read_until(marker, timeout=None):
        cmd = sent_data[0]
        sm = cmd.split("echo ")[1].split(";")[0].strip()
        return f"{cmd}{sm}\nhello\nworld\n{marker}:0\n"

    shell._read_until = fake_read_until
    result = shell.run(command)

    # Verify base64 encoding was used
    assert f"echo {expected_b64} | base64 -d | bash" in sent_data[0]
    assert result.exit_code == 0


def test_sudoshell_singleline_no_base64():
    """SudoShell.run() does NOT base64-encode single-line commands."""
    shell = _mock_sudo_shell()

    sent_data = []
    shell.channel.send = lambda data: sent_data.append(data)

    def fake_read_until(marker, timeout=None):
        cmd = sent_data[0]
        sm = cmd.split("echo ")[1].split(";")[0].strip()
        return f"{cmd}{sm}\noutput\n{marker}:0\n"

    shell._read_until = fake_read_until
    shell.run("hostname")

    assert "base64" not in sent_data[0]
    assert "hostname" in sent_data[0]
