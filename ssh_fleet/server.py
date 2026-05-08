"""ssh-fleet MCP server.

Exposes SSH tools to Claude Code via the Model Context Protocol.
"""
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ssh_fleet.machines import MachineStore
from ssh_fleet.pool import ConnectionPool
from ssh_fleet.ssh import SSHConnectionError
import tempfile

from ssh_fleet.terminal_render import (
    RenderOptions, render_terminal_svg, render_terminal_frames,
)

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
        " file operations (read_file, write_file, upload, download), staged"
        " script execution (run_script), remote tmux terminal control"
        " (tmux_new/list/capture/wait/paste/send_keys/kill), and machine inventory"
        " (list_machines, add_host, reload_machines)."
        " The 'host' parameter accepts hostname OR IP address."
        " IMPORTANT: Use sudo_exec (not exec) for kubectl, systemctl, and"
        " most system commands - regular users typically lack permissions."
        " IMPORTANT: Use run_script instead of sudo_exec for heredocs, long"
        " scripts, tmux launch wrappers, or commands with complex quoting."
    ),
)

# Config paths for reload
_status_url: Optional[str] = None
_auth_file: Optional[Path] = None
_guide_output_dir: Optional[Path] = None


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
    global _status_url, _auth_file, _guide_output_dir

    try:
        import yaml
    except ImportError:
        logger.error("pyyaml not installed")
        return

    config_path = _find_config()
    data = {}
    if config_path:
        logger.info(f"Using config: {config_path}")
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    else:
        logger.warning("No config found. Checked ~/.ssh-fleet/config.yaml and SSH_FLEET_CONFIG env var.")

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

    # Guide output directory for render_command. Always set, even if mkdir fails,
    # so the tool can surface a useful error on write.
    guide_dir = Path(data.get("guide_output_dir", "~/.ssh-fleet/guides")).expanduser()
    _guide_output_dir = guide_dir
    try:
        guide_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Guide output dir: {guide_dir}")
    except OSError as e:
        logger.warning(f"Cannot create guide output dir {guide_dir}: {e}")


def _slugify_command(command: str) -> str:
    """Slugify the first 40 chars of a command for use in a filename.

    Lowercases, replaces non-alphanumeric runs with '-', collapses repeats,
    and strips leading/trailing dashes. Falls back to 'cmd' if empty.
    """
    head = command[:40].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", head)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug or "cmd"


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


def _convert_svg_to_png(svg_path: Path, png_path: Optional[Path] = None) -> Path:
    """Convert an SVG to PNG using rsvg-convert (from librsvg).

    Produces a correctly-sized PNG at 1200px width, preserving the SVG's
    aspect ratio. If png_path is None, writes alongside the SVG with a
    .png suffix (used by the GIF frame pipeline). Otherwise writes to
    png_path (used by _save_png_and_report with a tempfile input).

    Raises RuntimeError with an actionable message if rsvg-convert is
    unavailable or the conversion fails.
    """
    if shutil.which("rsvg-convert") is None:
        raise RuntimeError(
            "PNG output requires rsvg-convert (from librsvg). "
            "Install with: brew install librsvg"
        )

    clean_png = png_path if png_path is not None else svg_path.with_suffix(".png")
    result = subprocess.run(
        ["rsvg-convert", "-w", "1200", str(svg_path), "-o", str(clean_png)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"rsvg-convert failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    if not clean_png.exists():
        raise RuntimeError(
            f"rsvg-convert reported success but {clean_png} was not created"
        )

    return clean_png


def _convert_frames_to_gif(
    frames: list[str],
    output_path: Path,
    line_delay_ms: int = 200,
    hold_ms: int = 3000,
    prompt_hold_ms: int = 500,
) -> Path:
    """Convert a list of SVG frame strings into an animated GIF.

    Pipeline: SVG strings -> temp SVG files -> PNG via rsvg-convert -> GIF via ffmpeg.
    Raises RuntimeError with actionable messages if tools are missing.
    """
    if shutil.which("rsvg-convert") is None:
        raise RuntimeError(
            "GIF output requires rsvg-convert (from librsvg) for SVG-to-PNG "
            "conversion. Install with: brew install librsvg"
        )
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "GIF output requires ffmpeg. Install with: brew install ffmpeg"
        )

    with tempfile.TemporaryDirectory(prefix="ssh-fleet-gif-") as tmpdir:
        tmp = Path(tmpdir)

        # Write SVG frames and convert each to PNG
        png_paths: list[Path] = []
        for i, svg_content in enumerate(frames):
            svg_file = tmp / f"frame_{i:04d}.svg"
            svg_file.write_text(svg_content, encoding="utf-8")
            png_file = _convert_svg_to_png(svg_file)
            png_paths.append(png_file)

        # Build ffmpeg concat file with per-frame durations
        concat_file = tmp / "concat.txt"
        lines: list[str] = []
        for i, png in enumerate(png_paths):
            lines.append(f"file '{png}'")
            if i == 0:
                duration = prompt_hold_ms / 1000.0
            elif i == len(png_paths) - 1:
                duration = hold_ms / 1000.0
            else:
                duration = line_delay_ms / 1000.0
            lines.append(f"duration {duration:.3f}")
        # ffmpeg concat requires the last file repeated for its duration to take effect
        lines.append(f"file '{png_paths[-1]}'")
        concat_file.write_text("\n".join(lines), encoding="utf-8")

        # Run ffmpeg with palette optimization for Catppuccin colors
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-vf", "split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer",
                "-loop", "0",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed (exit {result.returncode}): {result.stderr.strip()}"
            )

    return output_path


def _save_png_and_report(
    svg: str,
    command: str,
    output_name: Optional[str],
    exit_code: int,
    n_lines: int,
) -> str:
    """Render a PNG from an SVG string and return the standard result string.

    The SVG is written to a tempfile (auto-deleted), rsvg-convert produces the
    PNG in the guide output dir, and the final path is returned. No SVG file
    is left in the guide dir.

    Shared by render_command (live SSH) and render_output (offline/mocked).
    """
    if _guide_output_dir is None:
        return "ERROR: guide output directory not configured"

    if output_name:
        filename = f"{output_name}.png"
    else:
        filename = f"{_slugify_command(command)}-{int(time.time())}.png"
    png_path = _guide_output_dir / filename

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".svg", delete=True, encoding="utf-8",
    ) as tmp:
        tmp.write(svg)
        tmp.flush()
        try:
            _convert_svg_to_png(Path(tmp.name), png_path)
        except RuntimeError as e:
            return f"ERROR: {e}"
        except OSError as e:
            return f"ERROR: Cannot write PNG to guide output directory: {png_path} ({e})"

    return f"Rendered: {png_path} \n[exit_code: {exit_code}, {n_lines} lines of output]"


def _save_gif_and_report(
    frames: list[str],
    command: str,
    output_name: Optional[str],
    exit_code: int,
    n_lines: int,
    line_delay_ms: int = 200,
    hold_ms: int = 3000,
    prompt_hold_ms: int = 500,
) -> str:
    """Convert SVG frames to an animated GIF and return the standard result string.

    Shared by render_gif (live SSH) and render_gif_output (offline/mocked).
    """
    if _guide_output_dir is None:
        return "ERROR: guide output directory not configured"

    if output_name:
        filename = f"{output_name}.gif"
    else:
        filename = f"{_slugify_command(command)}-{int(time.time())}.gif"

    gif_path = _guide_output_dir / filename
    try:
        _convert_frames_to_gif(
            frames, gif_path,
            line_delay_ms=line_delay_ms,
            hold_ms=hold_ms,
            prompt_hold_ms=prompt_hold_ms,
        )
    except RuntimeError as e:
        return f"ERROR: {e}"

    return (
        f"Rendered: {gif_path} \n"
        f"[exit_code: {exit_code}, {n_lines} lines of output, "
        f"{len(frames)} frames]"
    )


@mcp_server.tool()
async def render_command(
    host: str,
    command: str,
    sudo: bool = True,
    title: Optional[str] = None,
    output_name: Optional[str] = None,
    timeout: int = 60,
    max_output_lines: int = 200,
    prompt_cwd: str = "~",
) -> str:
    """Run a command on a remote host and render the output as a polished PNG terminal screenshot.

    The PNG is saved to the configured guide output directory and the absolute
    path is returned. Use this for blog posts, tutorials, and documentation -
    the output shown is whatever the command actually printed on the remote machine.

    sudo defaults to true (matches ssh-fleet's bias toward root for system commands).

    Note: with sudo=True (the default), remote stderr is merged into stdout due
    to PTY allocation for sudo password handling. Use sudo=False when you want
    stderr to render in red.

    Requires rsvg-convert (brew install librsvg) for SVG-to-PNG conversion.

    Args:
        host: Hostname or IP (case-insensitive)
        command: Shell command to execute
        sudo: Run with sudo elevation (default true)
        title: Optional title bar text; default "user@host: ~"
        output_name: Optional filename stem (no extension); default is slugified command + timestamp
        timeout: Command timeout in seconds (default 60)
        max_output_lines: Max output lines to render before truncation (default 200).
            Raise this for long guides where every line matters.
        prompt_cwd: Working directory shown in the prompt (default "~"). Set this
            when the guide implies the operator is in a specific directory, e.g.
            "/opt/onwatch".
    """
    m = store.get(host)
    if not m:
        return f"ERROR: Machine '{host}' not found. Use list_machines to see available machines."

    try:
        if sudo:
            result = await pool.sudo_exec(m, command, timeout=timeout)
        else:
            result = await pool.exec(m, command, timeout=timeout)
    except SSHConnectionError as e:
        return f"ERROR: {e}"

    opts = RenderOptions(
        prompt_user=m.username,
        prompt_host=m.hostname,
        prompt_cwd=prompt_cwd,
        title=title or f"{m.username}@{m.hostname}: ~",
        sudo=sudo,
        max_output_lines=max_output_lines,
    )
    svg = render_terminal_svg(
        command, result.stdout, result.stderr, result.exit_code, opts
    )

    n_lines = len(result.stdout.splitlines()) + len(result.stderr.splitlines())
    return _save_png_and_report(
        svg, command, output_name, result.exit_code, n_lines,
    )


@mcp_server.tool()
async def render_output(
    command: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    prompt_user: str = "user",
    prompt_host: str = "host",
    prompt_cwd: str = "~",
    sudo: bool = False,
    title: Optional[str] = None,
    output_name: Optional[str] = None,
    max_output_lines: int = 200,
) -> str:
    """Render a terminal screenshot from literal text without running any command.

    Unlike render_command, this tool does NO SSH. You supply the command text,
    stdout, and stderr yourself, and get back a PNG terminal screenshot. Use
    this for:
      - Idealized "here's what success looks like" guide screenshots
      - Output you already captured from logs, journalctl, or a previous session
      - Commands that would take too long to reproduce live
      - Machines that aren't in the fleet or are unreachable at write-time

    The prompt is fully customizable (user/host/cwd/sudo) because there is no
    machine record to pull those from. This is the intended "mock a screenshot"
    tool for guide authors.

    Requires rsvg-convert (brew install librsvg) for SVG-to-PNG conversion.

    Args:
        command: Command text to show on the prompt line
        stdout: Literal stdout text to render (may be empty)
        stderr: Literal stderr text to render in red (may be empty)
        exit_code: Shown in the result summary; does not affect rendering
        prompt_user: Username shown in the prompt (default "user")
        prompt_host: Hostname shown in the prompt (default "host")
        prompt_cwd: Working directory shown in the prompt (default "~")
        sudo: Show yellow "sudo " prefix in the prompt (default false)
        title: Optional title bar text; default "prompt_user@prompt_host: ~"
        output_name: Optional filename stem (no extension); default is slugified command + timestamp
        max_output_lines: Max output lines to render before truncation (default 200)
    """
    opts = RenderOptions(
        prompt_user=prompt_user,
        prompt_host=prompt_host,
        prompt_cwd=prompt_cwd,
        title=title or f"{prompt_user}@{prompt_host}: ~",
        sudo=sudo,
        max_output_lines=max_output_lines,
    )
    svg = render_terminal_svg(command, stdout, stderr, exit_code, opts)

    n_lines = len(stdout.splitlines()) + len(stderr.splitlines())
    return _save_png_and_report(
        svg, command, output_name, exit_code, n_lines,
    )


@mcp_server.tool()
async def render_gif(
    host: str,
    command: str,
    sudo: bool = True,
    title: Optional[str] = None,
    output_name: Optional[str] = None,
    timeout: int = 60,
    max_output_lines: int = 50,
    prompt_cwd: str = "~",
    line_delay_ms: int = 200,
    hold_ms: int = 3000,
    prompt_hold_ms: int = 500,
    batch_lines: int = 1,
) -> str:
    """Run a command on a remote host and render the output as an animated GIF.

    Like render_command, but produces a looping GIF that shows the command
    being typed and output appearing line by line. Use for documentation
    and tutorials where animation helps convey the experience.

    max_output_lines defaults to 50 (not 200) because each line becomes a
    frame. GIFs with 200+ frames are large and slow to generate.

    Requires ffmpeg (brew install ffmpeg) and macOS qlmanage.

    Args:
        host: Hostname or IP (case-insensitive)
        command: Shell command to execute
        sudo: Run with sudo elevation (default true)
        title: Optional title bar text; default "user@host: ~"
        output_name: Optional filename stem (no extension)
        timeout: Command timeout in seconds (default 60)
        max_output_lines: Max output lines before truncation (default 50)
        prompt_cwd: Working directory shown in the prompt (default "~")
        line_delay_ms: Milliseconds between output line frames (default 200)
        hold_ms: Milliseconds to hold the final frame (default 3000)
        prompt_hold_ms: Milliseconds to hold the initial prompt frame (default 500)
        batch_lines: Lines to reveal per frame (default 1). Use 3+ for long output.
    """
    m = store.get(host)
    if not m:
        return f"ERROR: Machine '{host}' not found. Use list_machines to see available machines."

    try:
        if sudo:
            result = await pool.sudo_exec(m, command, timeout=timeout)
        else:
            result = await pool.exec(m, command, timeout=timeout)
    except SSHConnectionError as e:
        return f"ERROR: {e}"

    opts = RenderOptions(
        prompt_user=m.username,
        prompt_host=m.hostname,
        prompt_cwd=prompt_cwd,
        title=title or f"{m.username}@{m.hostname}: ~",
        sudo=sudo,
        max_output_lines=max_output_lines,
    )
    frames = render_terminal_frames(
        command, result.stdout, result.stderr, result.exit_code,
        opts, batch_lines=batch_lines,
    )

    n_lines = len(result.stdout.splitlines()) + len(result.stderr.splitlines())
    return _save_gif_and_report(
        frames, command, output_name, result.exit_code, n_lines,
        line_delay_ms=line_delay_ms,
        hold_ms=hold_ms,
        prompt_hold_ms=prompt_hold_ms,
    )


@mcp_server.tool()
async def render_gif_output(
    command: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    prompt_user: str = "user",
    prompt_host: str = "host",
    prompt_cwd: str = "~",
    sudo: bool = False,
    title: Optional[str] = None,
    output_name: Optional[str] = None,
    max_output_lines: int = 50,
    line_delay_ms: int = 200,
    hold_ms: int = 3000,
    prompt_hold_ms: int = 500,
    batch_lines: int = 1,
) -> str:
    """Render an animated GIF from literal text without running any command.

    Like render_output, but produces a looping animated GIF instead of a
    static screenshot. You supply the command text, stdout, and stderr,
    and get an animation showing the output appearing line by line.

    Use for mocked or pre-captured output where you want animation.
    Requires ffmpeg (brew install ffmpeg) and macOS qlmanage.

    Args:
        command: Command text to show on the prompt line
        stdout: Literal stdout text to render (may be empty)
        stderr: Literal stderr text to render in red (may be empty)
        exit_code: Shown in the result summary
        prompt_user: Username shown in the prompt (default "user")
        prompt_host: Hostname shown in the prompt (default "host")
        prompt_cwd: Working directory shown in the prompt (default "~")
        sudo: Show yellow "sudo " prefix in the prompt (default false)
        title: Optional title bar text; default "prompt_user@prompt_host: ~"
        output_name: Optional filename stem (no extension)
        max_output_lines: Max output lines before truncation (default 50)
        line_delay_ms: Milliseconds between output line frames (default 200)
        hold_ms: Milliseconds to hold the final frame (default 3000)
        prompt_hold_ms: Milliseconds to hold the initial prompt frame (default 500)
        batch_lines: Lines to reveal per frame (default 1). Use 3+ for long output.
    """
    opts = RenderOptions(
        prompt_user=prompt_user,
        prompt_host=prompt_host,
        prompt_cwd=prompt_cwd,
        title=title or f"{prompt_user}@{prompt_host}: ~",
        sudo=sudo,
        max_output_lines=max_output_lines,
    )
    frames = render_terminal_frames(
        command, stdout, stderr, exit_code, opts, batch_lines=batch_lines,
    )

    n_lines = len(stdout.splitlines()) + len(stderr.splitlines())
    return _save_gif_and_report(
        frames, command, output_name, exit_code, n_lines,
        line_delay_ms=line_delay_ms,
        hold_ms=hold_ms,
        prompt_hold_ms=prompt_hold_ms,
    )


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
async def write_file(host: str, remote_path: str, content: str,
                     sudo: bool = False, mode: str = "0644",
                     owner: str = "", timeout: int = 60) -> str:
    """Write text content to a remote file without shell heredocs.

    This is the safe MCP-compatible way to create remote files from Claude Code
    or Codex. The content is transferred through SFTP. With sudo=true, content
    is staged under /tmp and installed into place with a short root command, so
    the file body is not embedded in a fragile shell string.

    Args:
        host: Hostname or IP of the target machine (case-insensitive)
        remote_path: Absolute path to write on the remote machine
        content: UTF-8 text content to write
        sudo: Install as root after SFTP staging (default false)
        mode: Octal file mode such as 0644, 0600, or 0755
        owner: Optional owner or owner:group, requires sudo=true
        timeout: Seconds before timeout for the sudo install step
    """
    m = store.get(host)
    if not m:
        return (
            f"ERROR: Machine '{host}' not found."
            " Use list_machines to see available machines."
        )
    try:
        size = await pool.write_file(
            m,
            remote_path,
            content,
            sudo=sudo,
            mode=mode,
            owner=owner or None,
            timeout=timeout,
        )
        return f"Wrote {size:,} bytes to {host}:{remote_path}"
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except PermissionError:
        return f"ERROR: Permission denied writing to {remote_path}"
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


def _format_run_script_result(host: str, script_result) -> str:
    """Format staged script output for MCP clients without dumping metadata noise."""
    result = script_result.result
    if script_result.tmux_session:
        if result.exit_code != 0 or result.error:
            return result.format()

        lines = [
            (
                f"Launched script in tmux session "
                f"'{script_result.tmux_session}' on {host}."
            ),
        ]
        if script_result.log_path:
            lines.extend([
                f"log_path: {script_result.log_path}",
                f"follow_log: tail -f {script_result.log_path}",
            ])
        lines.append(f"attach: tmux attach -t {script_result.tmux_session}")
        if script_result.kept:
            lines.extend([
                f"script_path: {script_result.script_path}",
                f"runner_path: {script_result.runner_path}",
            ])
        return "\n".join(lines)

    lines = [result.format()]
    if script_result.log_path:
        lines.append(f"log_path: {script_result.log_path}")
    if script_result.kept:
        lines.extend([
            f"script_path: {script_result.script_path}",
            f"runner_path: {script_result.runner_path}",
        ])
    return "\n".join(lines)


@mcp_server.tool()
async def run_script(host: str, script: str, sudo: bool = True,
                     timeout: int = 60, tmux_session: Optional[str] = None,
                     log_path: Optional[str] = None, env: Optional[dict] = None,
                     keep_script: bool = False) -> str:
    """Stage and run a bash script on a remote machine.

    Use this instead of exec/sudo_exec for heredocs, multi-line scripts, tmux
    launch wrappers, installers, or commands with complex quoting. The script
    body and small runner are transferred through SFTP, then ssh-fleet executes
    only a short command. This avoids JSON/string/newline escaping failures in
    Claude Code and other MCP clients.

    Args:
        host: Hostname or IP of the target machine (case-insensitive)
        script: Bash script content to stage and run
        sudo: Run as root through sudo (default true)
        timeout: Seconds before timeout for synchronous runs or tmux launch
        tmux_session: Optional tmux session name. If set, launch and return.
        log_path: Optional absolute path for tee'd output and EXIT marker
        env: Optional JSON object of environment variables for the script
        keep_script: Keep staged files after completion. If env contains
            secrets, leave this false.
    """
    m = store.get(host)
    if not m:
        return (
            f"ERROR: Machine '{host}' not found."
            " Use list_machines to see available machines."
        )
    if env is not None and not isinstance(env, dict):
        return "ERROR: env must be a JSON object of KEY: value pairs"

    try:
        result = await pool.run_script(
            m,
            script,
            sudo=sudo,
            timeout=timeout,
            tmux_session=tmux_session or None,
            log_path=log_path or None,
            env=env,
            keep_script=keep_script,
        )
        return _format_run_script_result(host, result)
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def _machine_or_error(host: str):
    m = store.get(host)
    if not m:
        return None, (
            f"ERROR: Machine '{host}' not found."
            " Use list_machines to see available machines."
        )
    return m, ""


@mcp_server.tool()
async def tmux_new(host: str, session: str, command: str = "",
                   cwd: str = "", sudo: bool = True,
                   timeout: int = 30) -> str:
    """Create a detached tmux session on a remote machine.

    Use this when an agent needs a durable remote terminal it can control over
    multiple MCP calls. Start an empty shell by leaving command blank, or start
    a command like "bash" or "top". Follow with tmux_capture, tmux_paste, and
    tmux_send_keys.

    Args:
        host: Hostname or IP of the target machine
        session: tmux session name, letters/numbers/._:- only
        command: Optional command to run in the session
        cwd: Optional absolute working directory for the session
        sudo: Create the tmux session as root via sudo (default true)
        timeout: Seconds before timeout
    """
    m, error = _machine_or_error(host)
    if error:
        return error
    try:
        result = await pool.tmux_new(
            m, session, command=command, cwd=cwd, sudo=sudo, timeout=timeout
        )
        if result.exit_code == 0:
            return f"Started tmux session '{session}' on {host}."
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def tmux_list(host: str, sudo: bool = True, timeout: int = 20) -> str:
    """List tmux sessions on a remote machine.

    Output columns are: session name, window count, attached count, created time.
    Args:
        host: Hostname or IP of the target machine
        sudo: List root-owned tmux sessions via sudo (default true)
        timeout: Seconds before timeout
    """
    m, error = _machine_or_error(host)
    if error:
        return error
    try:
        result = await pool.tmux_list(m, sudo=sudo, timeout=timeout)
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def tmux_capture(host: str, target: str, lines: int = 200,
                       sudo: bool = True, timeout: int = 20) -> str:
    """Capture recent output from a remote tmux pane.

    Use this as the main observation tool after tmux_new, tmux_paste, or
    tmux_send_keys. Target can be a session, session:window.pane, or %pane id.

    Args:
        host: Hostname or IP of the target machine
        target: tmux target such as session, session:0.0, or %1
        lines: Number of recent lines to capture
        sudo: Capture root-owned tmux sessions via sudo (default true)
        timeout: Seconds before timeout
    """
    m, error = _machine_or_error(host)
    if error:
        return error
    try:
        result = await pool.tmux_capture(
            m, target, lines=lines, sudo=sudo, timeout=timeout
        )
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def tmux_wait(host: str, target: str, pattern: str,
                    regex: bool = False, timeout: int = 60,
                    interval: float = 1.0, lines: int = 200,
                    sudo: bool = True) -> str:
    """Wait until text appears in a remote tmux pane.

    Use this after tmux_paste or tmux_send_keys when an agent needs to wait for
    a prompt, completion message, menu, or error before deciding the next step.
    Returns the latest captured pane output with MATCHED or TIMEOUT status.

    Args:
        host: Hostname or IP of the target machine
        target: tmux target such as session, session:0.0, or %1
        pattern: Literal text or regex to wait for
        regex: Treat pattern as a Python regex instead of literal text
        timeout: Maximum seconds to wait
        interval: Seconds between captures
        lines: Number of recent lines to capture each poll
        sudo: Capture root-owned tmux sessions via sudo (default true)
    """
    m, error = _machine_or_error(host)
    if error:
        return error
    try:
        result = await pool.tmux_wait(
            m,
            target,
            pattern,
            regex=regex,
            timeout=timeout,
            interval=interval,
            lines=lines,
            sudo=sudo,
        )
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def tmux_paste(host: str, target: str, text: str,
                     enter: bool = False, sudo: bool = True,
                     timeout: int = 20) -> str:
    """Paste arbitrary text into a remote tmux pane.

    Text is staged through SFTP as a tmux buffer, so quotes, dollar signs,
    heredocs, and multiline commands are not embedded in the SSH command.
    Set enter=true to send Enter after the paste.

    Args:
        host: Hostname or IP of the target machine
        target: tmux target such as session, session:0.0, or %1
        text: Text to paste into the pane
        enter: Send Enter after pasting (default false)
        sudo: Paste into root-owned tmux sessions via sudo (default true)
        timeout: Seconds before timeout
    """
    m, error = _machine_or_error(host)
    if error:
        return error
    try:
        result = await pool.tmux_paste(
            m, target, text, enter=enter, sudo=sudo, timeout=timeout
        )
        if result.exit_code == 0:
            return f"Pasted {len(text)} characters to tmux target '{target}' on {host}."
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def tmux_send_keys(host: str, target: str, keys: str,
                         sudo: bool = True, timeout: int = 20) -> str:
    """Send tmux key tokens to a remote pane.

    Use this for controls like C-c, Enter, Up, Down, or C-m. For normal text,
    especially text with spaces or shell metacharacters, use tmux_paste.

    Args:
        host: Hostname or IP of the target machine
        target: tmux target such as session, session:0.0, or %1
        keys: Whitespace-separated tmux key tokens, for example "C-c" or "Enter"
        sudo: Send keys to root-owned tmux sessions via sudo (default true)
        timeout: Seconds before timeout
    """
    m, error = _machine_or_error(host)
    if error:
        return error
    try:
        result = await pool.tmux_send_keys(
            m, target, keys, sudo=sudo, timeout=timeout
        )
        if result.exit_code == 0:
            return f"Sent keys to tmux target '{target}' on {host}: {keys}"
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp_server.tool()
async def tmux_kill(host: str, target: str,
                    sudo: bool = True, timeout: int = 20) -> str:
    """Kill a remote tmux session.

    Args:
        host: Hostname or IP of the target machine
        target: tmux session target to kill
        sudo: Kill root-owned tmux sessions via sudo (default true)
        timeout: Seconds before timeout
    """
    m, error = _machine_or_error(host)
    if error:
        return error
    try:
        result = await pool.tmux_kill(m, target, sudo=sudo, timeout=timeout)
        if result.exit_code == 0:
            return f"Killed tmux target '{target}' on {host}."
        return result.format()
    except SSHConnectionError as e:
        return f"ERROR: {e}"
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
                   permanent: bool = True,
                   overwrite: bool = False) -> str:
    """Add a machine to the fleet. Defaults to permanent (saved to the hosts file).

    The machine is available immediately for exec/sudo_exec.
    With permanent=True (default), it's appended to the hosts file and persists across sessions.
    Pass permanent=False for a session-only entry (required when using key_file auth).
    Pass overwrite=True to replace any existing permanent row with the same hostname or IP
    (useful when the same IP is reused for a different physical machine).

    Args:
        hostname: Name for this machine
        ip: IP address
        username: SSH username
        password: SSH password (required for permanent; optional if using key_file for temp)
        key_file: Path to SSH private key (only for temporary machines; pass permanent=False)
        permanent: Save to hosts file for future sessions (default true)
        overwrite: Replace existing rows matching this hostname or IP (permanent only, default false)
    """
    try:
        if permanent:
            m = store.add_permanent(
                hostname, ip, username, password=password,
                auth_file=_auth_file, overwrite=overwrite,
            )
            suffix = " (replaced existing entry)" if overwrite else ""
            return f"Added '{hostname}' ({ip}) permanently to {_auth_file}{suffix}. Ready for exec/sudo_exec."
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
