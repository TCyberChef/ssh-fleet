"""Integration tests for the render_command MCP tool.

The pool and store are replaced with isolated test doubles via monkeypatch,
so these tests exercise server.render_command end-to-end without touching
paramiko or the real connection pool.
"""
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ssh_fleet import server
from ssh_fleet.machines import MachineStore
from ssh_fleet.ssh import CommandResult, SSHConnectionError


@pytest.fixture
def server_env(tmp_path, monkeypatch):
    """Swap server module globals with isolated test doubles per test."""
    store = MachineStore()
    pool = MagicMock()
    pool.sudo_exec = AsyncMock()
    pool.exec = AsyncMock()
    guide_dir = tmp_path / "guides"
    guide_dir.mkdir()

    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "pool", pool)
    monkeypatch.setattr(server, "_guide_output_dir", guide_dir)

    return {"store": store, "pool": pool, "guide_dir": guide_dir}


@pytest.fixture
def fake_rsvg(monkeypatch):
    """Install a fake rsvg-convert so PNG rendering works offline.

    Returns a list that gets appended with the SVG content each time
    rsvg-convert is invoked, so tests can still assert on the SVG
    (no SVG file is left on disk after the render).
    """
    captured: list[str] = []
    _install_fake_rsvg(monkeypatch, present=True, captured=captured)
    return captured


@pytest.mark.asyncio
async def test_render_command_unknown_host_returns_exact_error(server_env):
    """Unknown host returns the exact error string used by other tools."""
    result = await server.render_command("ghost", "ls")
    assert result == (
        "ERROR: Machine 'ghost' not found."
        " Use list_machines to see available machines."
    )
    # No pool call, no file written
    server_env["pool"].sudo_exec.assert_not_called()
    assert list(server_env["guide_dir"].iterdir()) == []


@pytest.mark.asyncio
async def test_render_command_success_writes_png_and_returns_path(server_env, fake_rsvg):
    """Successful call writes a PNG file and returns its path + exit code summary."""
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].sudo_exec.return_value = CommandResult(
        stdout="hello world", stderr="", exit_code=0
    )

    result = await server.render_command("web-1", "echo hello")

    assert "Rendered:" in result
    assert ".png" in result
    assert "exit_code: 0" in result

    pngs = list(server_env["guide_dir"].glob("*.png"))
    assert len(pngs) == 1
    # No SVG is left on disk anymore
    assert list(server_env["guide_dir"].glob("*.svg")) == []
    # Inspect the SVG content captured during rsvg-convert
    assert len(fake_rsvg) == 1
    content = fake_rsvg[0]
    assert "<svg" in content
    assert "hello world" in content
    assert "echo hello" in content
    # The prompt shows the real username from the Machine record
    assert "admin" in content
    assert "web-1" in content


@pytest.mark.asyncio
async def test_render_command_ssh_error_returns_error_and_writes_no_file(server_env, fake_rsvg):
    """SSHConnectionError from pool → ERROR return, no crash, no file."""
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].sudo_exec.side_effect = SSHConnectionError("connection refused")

    result = await server.render_command("web-1", "ls")

    assert result.startswith("ERROR:")
    assert "connection refused" in result
    assert list(server_env["guide_dir"].glob("*.png")) == []
    assert list(server_env["guide_dir"].glob("*.svg")) == []
    # SSH failed before we ever rendered, so rsvg-convert was never invoked
    assert fake_rsvg == []


@pytest.mark.asyncio
async def test_render_command_respects_max_output_lines(server_env, fake_rsvg):
    """max_output_lines kwarg overrides the renderer default and truncates stdout."""
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    stdout = "\n".join(f"row {i}" for i in range(20))
    server_env["pool"].sudo_exec.return_value = CommandResult(
        stdout=stdout, stderr="", exit_code=0
    )

    await server.render_command("web-1", "seq 20", max_output_lines=5)

    svg = fake_rsvg[0]
    # keep = max - 1 = 4 → rows 0..3 visible, rest truncated
    assert ">row 0<" in svg
    assert ">row 3<" in svg
    assert "truncated" in svg.lower()
    assert ">row 10<" not in svg
    assert ">row 19<" not in svg


@pytest.mark.asyncio
async def test_render_command_respects_prompt_cwd(server_env, fake_rsvg):
    """prompt_cwd kwarg renders a custom working directory in the prompt line."""
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].sudo_exec.return_value = CommandResult(
        stdout="done", stderr="", exit_code=0
    )

    await server.render_command("web-1", "ls", prompt_cwd="/opt/onwatch")

    svg = fake_rsvg[0]
    # The cwd tspan is the blue segment in the prompt
    assert '>/opt/onwatch</tspan>' in svg


@pytest.mark.asyncio
async def test_render_output_writes_png_from_literal_text(server_env, fake_rsvg):
    """render_output renders literal command/stdout text without touching SSH."""
    result = await server.render_output(
        command="kubectl get pods",
        stdout="NAME     STATUS\npod-a    Running",
        stderr="",
        exit_code=0,
    )

    assert "Rendered:" in result
    pngs = list(server_env["guide_dir"].glob("*.png"))
    assert len(pngs) == 1
    assert list(server_env["guide_dir"].glob("*.svg")) == []
    content = fake_rsvg[0]
    assert "kubectl get pods" in content
    assert "pod-a" in content
    assert "Running" in content


@pytest.mark.asyncio
async def test_render_output_does_not_touch_pool(server_env, fake_rsvg):
    """render_output must be offline — no SSH calls to pool.exec / sudo_exec."""
    await server.render_output(command="echo hi", stdout="hi")

    server_env["pool"].exec.assert_not_called()
    server_env["pool"].sudo_exec.assert_not_called()


@pytest.mark.asyncio
async def test_render_output_stderr_renders_in_red(server_env, fake_rsvg):
    """render_output with non-empty stderr gets the red fill-opacity treatment."""
    await server.render_output(
        command="cat /missing",
        stdout="",
        stderr="cat: /missing: No such file",
        exit_code=1,
    )

    svg = fake_rsvg[0]
    assert "No such file" in svg
    assert 'fill-opacity="0.85"' in svg


@pytest.mark.asyncio
async def test_render_output_supports_custom_prompt(server_env, fake_rsvg):
    """render_output accepts prompt_user/host/cwd/sudo for fully mocked screenshots."""
    await server.render_output(
        command="systemctl status onwatch",
        stdout="active (running)",
        prompt_user="root",
        prompt_host="nissan",
        prompt_cwd="/etc/onwatch",
        sudo=True,
    )

    svg = fake_rsvg[0]
    assert "root" in svg
    assert "nissan" in svg
    assert '>/etc/onwatch</tspan>' in svg
    # sudo=True → yellow marker (Catppuccin YELLOW)
    assert "#f9e2af" in svg


def _install_fake_rsvg(monkeypatch, present: bool = True, captured=None):
    """Stub shutil.which + subprocess.run so PNG rendering works without rsvg-convert.

    When present=True: shutil.which reports rsvg-convert, and subprocess.run
    writes a fake PNG at the -o path.
    When present=False: shutil.which returns None → helper should error cleanly.
    When captured is a list: the SVG content passed to rsvg-convert (argv[3])
    is read and appended, so tests can still assert on the rendered SVG even
    though no SVG file is left behind on disk.
    """
    def fake_which(name):
        if present and name == "rsvg-convert":
            return "/opt/homebrew/bin/rsvg-convert"
        return None

    def fake_run(cmd, **kwargs):
        if cmd[0] == "rsvg-convert":
            # rsvg-convert -w 1200 input.svg -o output.png
            input_svg = Path(cmd[3])
            if captured is not None and input_svg.exists():
                captured.append(input_svg.read_text())
            out_idx = cmd.index("-o")
            Path(cmd[out_idx + 1]).write_bytes(b"fake-png-bytes")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)


@pytest.mark.asyncio
async def test_render_returns_error_when_rsvg_missing(server_env, monkeypatch):
    """Missing rsvg-convert returns a helpful ERROR with install instructions."""
    _install_fake_rsvg(monkeypatch, present=False)
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].sudo_exec.return_value = CommandResult(
        stdout="hello", stderr="", exit_code=0
    )

    result = await server.render_command("web-1", "echo hello")

    assert result.startswith("ERROR:")
    assert "rsvg-convert" in result
    assert "brew" in result
    # No PNG was produced when rsvg-convert is missing
    assert list(server_env["guide_dir"].glob("*.png")) == []


# --- GIF pipeline tests ---


def _install_fake_rsvg_and_ffmpeg(monkeypatch, rsvg=True, ffmpeg=True):
    """Stub shutil.which + subprocess.run for both rsvg-convert and ffmpeg.

    rsvg-convert: simulates PNG creation from SVG files.
    ffmpeg: simulates GIF creation from the concat file.
    """
    def fake_which(name):
        if rsvg and name == "rsvg-convert":
            return "/opt/homebrew/bin/rsvg-convert"
        if ffmpeg and name == "ffmpeg":
            return "/opt/homebrew/bin/ffmpeg"
        return None

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "rsvg-convert":
            out_idx = cmd.index("-o")
            Path(cmd[out_idx + 1]).write_bytes(b"fake-png")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        elif cmd[0] == "ffmpeg":
            output_path = Path(cmd[-1])
            output_path.write_bytes(b"GIF89a-fake")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)


@pytest.mark.asyncio
async def test_render_gif_unknown_host_returns_error(server_env):
    """Unknown host returns the standard error message."""
    result = await server.render_gif("ghost", "ls")
    assert result.startswith("ERROR:")
    assert "ghost" in result
    server_env["pool"].sudo_exec.assert_not_called()


@pytest.mark.asyncio
async def test_render_gif_success(server_env, monkeypatch):
    """Full GIF pipeline with mocked qlmanage + ffmpeg produces a .gif file."""
    _install_fake_rsvg_and_ffmpeg(monkeypatch)
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].sudo_exec.return_value = CommandResult(
        stdout="line 1\nline 2\nline 3", stderr="", exit_code=0
    )

    result = await server.render_gif("web-1", "echo test")

    assert "Rendered:" in result
    assert ".gif" in result
    assert "3 lines" in result
    assert "frames" in result

    gifs = list(server_env["guide_dir"].glob("*.gif"))
    assert len(gifs) == 1
    assert gifs[0].read_bytes() == b"GIF89a-fake"


@pytest.mark.asyncio
async def test_render_gif_output_success(server_env, monkeypatch):
    """render_gif_output (mock version) produces a GIF without SSH calls."""
    _install_fake_rsvg_and_ffmpeg(monkeypatch)

    result = await server.render_gif_output(
        command="kubectl get pods",
        stdout="NAME  READY\npod-1  1/1",
        output_name="test-gif",
    )

    assert "Rendered:" in result
    assert "test-gif.gif" in result
    server_env["pool"].exec.assert_not_called()
    server_env["pool"].sudo_exec.assert_not_called()

    gifs = list(server_env["guide_dir"].glob("*.gif"))
    assert len(gifs) == 1


@pytest.mark.asyncio
async def test_render_gif_no_ffmpeg(server_env, monkeypatch):
    """Missing ffmpeg returns an actionable error with install instructions."""
    _install_fake_rsvg_and_ffmpeg(monkeypatch, rsvg=True, ffmpeg=False)
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].sudo_exec.return_value = CommandResult(
        stdout="hi", stderr="", exit_code=0
    )

    result = await server.render_gif("web-1", "echo hi")

    assert result.startswith("ERROR:")
    assert "ffmpeg" in result
    assert "brew" in result


@pytest.mark.asyncio
async def test_render_gif_no_rsvg(server_env, monkeypatch):
    """Missing rsvg-convert returns an actionable error."""
    _install_fake_rsvg_and_ffmpeg(monkeypatch, rsvg=False, ffmpeg=True)
    server_env["store"].add_temporary("web-1", "1.2.3.4", "admin", password="pw")
    server_env["pool"].sudo_exec.return_value = CommandResult(
        stdout="hi", stderr="", exit_code=0
    )

    result = await server.render_gif("web-1", "echo hi")

    assert result.startswith("ERROR:")
    assert "rsvg-convert" in result
    assert "brew" in result
