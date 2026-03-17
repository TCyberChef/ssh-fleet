"""Tests for SSH connection wrapper."""
import pytest
from unittest.mock import MagicMock, patch, mock_open
from ssh_fleet.ssh import CommandResult, SSHConnection, SSHConnectionError, escape_for_shell, strip_sudo_prompt


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
