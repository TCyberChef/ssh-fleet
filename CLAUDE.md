# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

ssh-fleet is an MCP server that provides SSH access to a fleet of machines from Claude Code. Single Python process, all machines, connection pooling, persistent shells. Registered globally with `--scope user`.

## Commands

```bash
# Activate venv
source .venv/bin/activate

# Install in dev mode
pip install -e ".[test]"

# Run all tests
pytest tests/ -v

# Run a single test
pytest tests/test_machines.py::test_parse_machine_line -v

# Run the server directly (for debugging - normally launched by Claude Code via stdio)
ssh-fleet
```

## Architecture

Five modules in `ssh_fleet/`:

- **`server.py`** - MCP tool definitions using FastMCP. Each `@mcp_server.tool()` function is a tool Claude Code can call. Global `store` (MachineStore), `pool` (ConnectionPool), and `_guide_output_dir` are module-level singletons.
- **`ssh.py`** - Low-level SSH via paramiko. Two execution paths:
  - `SSHConnection.run()` / `run_sudo()` - wraps commands in `bash -l -c` (login shell) via `shlex.quote` for full PATH and multi-line safety. Each call is a separate SSH channel.
  - `SudoShell` - uses `invoke_shell()` for persistent interactive shell. Elevates to root once via `sudo su -`. Uses start/end marker pairs for reliable output parsing. Multi-line commands are base64-encoded to avoid interactive shell quoting issues.
- **`pool.py`** - Connection pool with per-machine asyncio locks, idle cleanup (runs every 60s), and shell session management. Bridges sync paramiko calls to async via `asyncio.to_thread()`. Dead shells are auto-cleaned with actionable error messages.
- **`machines.py`** - Machine inventory from hosts file (tab-separated), optional dashboard metadata enrichment, temporary (session-only) and permanent machine registration. Lookup by hostname or IP address.
- **`terminal_render.py`** - Pure stdlib SVG renderer. Data in (command + stdout + stderr + exit_code + `RenderOptions`), SVG string out. No SSH, no MCP, no async. Catppuccin Mocha palette, macOS window chrome, f-string generated. Used by `render_command` and `render_gif` tools. Also contains `render_terminal_frames()` for generating progressive SVG frames (animated GIF support).

### exec vs shell: Key Difference

`exec` / `sudo_exec` use paramiko's `exec_command()`:
- Wrapped in `bash -l -c` for login shell PATH
- Each call is independent (no state between calls)
- Commands are `shlex.quote`-wrapped, so multi-line and special chars are safe

`shell_open` / `shell_run` use `invoke_shell()`:
- State persists between `shell_run` calls (cd, env vars, etc.)
- Already elevated to root (no sudo overhead per command)
- Multi-line commands are base64-encoded and piped to bash
- Single-line commands run directly with start/end marker parsing

### Connection Resilience

- SSH keepalives every 30s detect dead connections within ~60s
- Background cleanup loop closes connections idle >5min and shells idle >30min
- `shell_run` auto-detects dead shells, cleans up, and returns an error telling the caller to re-open
- `exec`/`sudo_exec` retry once on transient connection failures (not on auth errors)

### Output Formatting

`CommandResult.format()` auto-detects and pretty-prints JSON in stdout for LLM readability. Works on both pure JSON output and mixed text+JSON (per-line detection for lines >80 chars that look like complete JSON objects). Output is truncated at 50K chars. Failed commands with no output show `(no output)` hint.

### Guide rendering (`render_command`)

The `render_command` tool runs a command through the existing `ConnectionPool` and renders its real output as a self-contained SVG terminal screenshot (Catppuccin Mocha, macOS window chrome, drop shadow, colored prompt). Pure stdlib - no `cairosvg`, no Playwright, no new dependencies. The pure renderer lives in `ssh_fleet/terminal_render.py` and is trivially unit-testable; the MCP tool in `server.py` wires it to the pool and writes the result to `_guide_output_dir` (configurable via `guide_output_dir` in `~/.ssh-fleet/config.yaml`, defaults to `~/.ssh-fleet/guides`). ANSI handling reuses `_strip_ansi` from `ssh.py` and layers a supplementary regex for bare C1 controls (like `\x1b=` / `\x1b>` emitted by `systemctl status` under a pty) that the shared regex doesn't catch - the fix lives in the renderer, not in `ssh.py`, to keep `SudoShell`'s marker-parsing behavior untouched. **Tip:** for guides that highlight errors, pass `sudo=False` - the default sudo path merges stderr into stdout at the PTY level, so the red-stderr rendering only triggers on the non-sudo path.

### Animated GIF rendering (`render_gif`)

The `render_gif` and `render_gif_output` tools produce looping animated GIFs showing commands being typed and output appearing line by line. Built on the same renderer as static screenshots (visual consistency guaranteed). The pipeline: `render_terminal_frames()` generates progressive SVG frames (each with `min_height` set for uniform dimensions), `rsvg-convert` converts each to PNG, ffmpeg stitches them into a GIF with per-frame timing (prompt hold, line delay, final hold). **External requirements:** `brew install librsvg ffmpeg`. The `batch_lines` parameter controls how many lines appear per frame (use 3+ for long output to keep GIFs reasonable). `max_output_lines` defaults to 50 (not 200) since each line becomes a frame. Note: `rsvg-convert` also powers the existing `fmt='png'` for static screenshots (replaced qlmanage which produced broken square-canvas output).

### Tool Parameters

All tools that target a machine use a `host` parameter (not `machine`). Accepts hostname or IP address, case-insensitive. The `add_host` tool supports both temporary (session-only) and permanent (appended to hosts file) machine registration.

## Testing

Tests use pytest + pytest-asyncio. Paramiko is mocked (no real SSH connections in tests). Pool tests use `@pytest.mark.asyncio` and `pytest_asyncio.fixture`.

## Config

The server reads config from `~/.ssh-fleet/config.yaml` (or `SSH_FLEET_CONFIG` env var, or falls back to `~/.onwatch-debug/config.yaml`). Config keys: `auth_file` (path to hosts TSV), `status_url` (optional dashboard JSON endpoint).
