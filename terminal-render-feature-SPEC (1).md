# Feature Spec: Terminal Screenshot Rendering in ssh-fleet

> Implementation spec for adding real-terminal SVG rendering to the existing `ssh-fleet` MCP server. Designed to be handed to Claude Code in one session.

---

## How to hand this off

From the `ssh-fleet` repo root:

```bash
claude -n terminal-render
```

Then paste this spec as the first message with this preamble:

```
ultrathink. Read this spec and implement it. Follow the Explore phase first,
then go straight to Implement — no separate planning document. Stop at each
checkpoint and show me what you have before continuing.

[paste spec]
```

The `ultrathink` keyword bumps reasoning effort for the session. `-n terminal-render` names it so you can `claude --resume terminal-render` later.

---

## TL;DR

Add a `render_command` MCP tool that runs a command via the existing `ConnectionPool` and produces a **Catppuccin-styled SVG terminal screenshot** of the real output. Pure stdlib, zero new deps. The SVG drops into blog posts, MDX, or markdown guides.

**Pipeline:** tool call → `pool.sudo_exec` → `CommandResult` → SVG renderer → file on disk → return path.

---

## Critical Constraints (read these twice)

These are non-negotiable. Violating any of them fails the spec.

1. **IMPORTANT: No new runtime dependencies.** Pure Python stdlib only. No Playwright, no Puppeteer, no `cairosvg`, no `html2image`. If you think you need a dep, you don't — use stdlib.
2. **IMPORTANT: Reuse `_strip_ansi` from `ssh_fleet/ssh.py`.** Do not duplicate the regex. If it isn't importable, export it.
3. **IMPORTANT: Do not modify existing tools** (`exec`, `sudo_exec`, `shell_*`) or `CommandResult`. Read `stdout` / `stderr` / `exit_code` directly — do not call `.format()`, it prettifies JSON and we want raw terminal output.
4. **IMPORTANT: The `host` parameter is named `host`, not `machine`.** Match the existing tools exactly.
5. **YOU MUST run `pytest tests/ -v` and see it pass before declaring done.** No "tests should pass" — actually run them.

---

## Explore Phase

**Do this in the main session, not a subagent** — you'll need the context for implementation. Read these files with `@`:

- `@ssh_fleet/server.py` — tool decorator pattern, error-string format, how `store`/`pool`/`_auth_file` are wired
- `@ssh_fleet/pool.py` — `pool.exec(machine, command, timeout)` and `pool.sudo_exec(...)` signatures, both return `CommandResult`
- `@ssh_fleet/ssh.py` — `CommandResult` fields (`stdout`, `stderr`, `exit_code`, `error`) and location of `_strip_ansi`
- `@ssh_fleet/machines.py` — `Machine` fields we use for the prompt line (`hostname`, `username`)
- `@tests/test_pool.py` and `@tests/test_ssh.py` — test style to match (mocked paramiko, `@pytest.mark.asyncio`)
- `@CLAUDE.md` — project-wide conventions
- `@pyproject.toml` — confirm current deps

**Checkpoint 1 — STOP HERE and show me:** 3–5 bullets confirming what you found. Specifically: (a) exact location of `_strip_ansi`, (b) exact signature of `pool.sudo_exec`, (c) exact `CommandResult` field names, (d) one line about the error-return convention. Then wait for me to say "go" before implementing. This is the Explore → Implement handoff; don't skip it.

---

## Design Decisions (already made — do not re-litigate)

- **Output format:** SVG only. Not PNG. Not HTML. Pure SVG, f-string generated.
- **Single combined tool:** `render_command(host, command, sudo=True, ...)` runs *and* renders in one call. Don't split into "run" + "render".
- **Sudo defaults to `True`** — matches the rest of ssh-fleet (`CLAUDE.md` notes most system commands need root).
- **Palette:** Catppuccin Mocha. Fixed. No theme system.
- **ANSI handling:** strip only. Don't parse colors into SVG in v1.
- **Failure case still renders:** A non-zero exit with stderr is valid content for a guide. Render it.
- **No template engine:** f-strings. Handlebars/Jinja is overkill.

---

## Files to Create / Modify

**New:**
- `ssh_fleet/terminal_render.py` — pure renderer, no SSH/MCP/async
- `tests/test_terminal_render.py` — unit tests for the renderer
- `tests/test_server_render.py` — integration test for the tool (mocked pool)

**Modified:**
- `ssh_fleet/server.py` — add `render_command` tool + `_guide_output_dir` config
- `ssh_fleet/ssh.py` — export `_strip_ansi` if not already
- `CLAUDE.md` — one paragraph about the new tool
- `README.md` — short usage example

**Untouched:** `pool.py`, `machines.py`, `pyproject.toml` (no new deps).

---

## `ssh_fleet/terminal_render.py` — Renderer

Pure function. No SSH, no MCP, no async. Data in → SVG string out. Trivially unit-testable.

### Public API

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class RenderOptions:
    title: Optional[str] = None          # default: "user@host: ~"
    prompt_user: str = "user"
    prompt_host: str = "host"
    prompt_cwd: str = "~"
    max_output_lines: int = 200
    max_line_chars: int = 140
    font_size: int = 14
    sudo: bool = False                    # visual "sudo " prefix

def render_terminal_svg(
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    opts: RenderOptions,
) -> str:
    """Return a complete SVG document as a string."""
```

### Visual spec

**Catppuccin Mocha palette:**
- `base` (bg): `#1e1e2e`
- `text` (fg): `#cdd6f4`
- `surface0` (title bar): `#313244`
- `green` (user@host): `#a6e3a1`
- `blue` (cwd): `#89b4fa`
- `red` (stderr / error): `#f38ba8`
- `yellow` (sudo marker): `#f9e2af`
- `overlay0` (muted / truncation): `#6c7086`

**Window chrome:**
- Rounded corners: `rx=10`
- Title bar: 28px, `surface0` background
- Traffic lights top-left: red `#ff5f57`, yellow `#febc2e`, green `#28c840`, radius 6, spacing 20px, inset 16px from left
- Title text centered in title bar, `#9399b2`, 12px
- Drop shadow via `<filter>` with `feGaussianBlur` (stdDev 4) + `feOffset` (dy 4)

**Body:**
- Font: `ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Consolas, monospace`
- Font size: `opts.font_size` (default 14)
- Line height: `font_size * 1.5`
- Padding: 20px all sides
- Char width estimate for sizing: `font_size * 0.6`

**Prompt line:** `user@host` green, `:` text, `cwd` blue, `$ ` text, command text. If `opts.sudo`, prefix visually with `sudo ` in yellow.

**Output:** stdout in text color, stderr in red with `fill-opacity="0.85"`, stdout first then stderr.

**Truncation:** if total lines > `max_output_lines`, keep first `(max - 1)` and append `... (N lines truncated)` in `overlay0`.

**Long lines:** soft-wrap at `max_line_chars`. SVG has no auto-wrap — you must break manually into multiple `<text>` rows.

**Sizing:** width = `max(line_widths) + 2*padding`, clamped `[600, 1400]`. Height = `28 + padding_top + (n_rows * line_height) + padding_bottom`. `viewBox` matches exactly.

### Implementation rules

- f-strings only. No templating engine.
- **IMPORTANT:** escape user content with `html.escape(text, quote=False)` before embedding in `<text>` elements. Test with inputs containing `<`, `>`, `&`.
- Use `xml:space="preserve"` on text elements.
- Each row is its own `<text>` at `y = 28 + padding + (row_index * line_height) + baseline`.
- Strip ANSI via imported `_strip_ansi` from `ssh_fleet/ssh.py`. Also strip `\r` and control chars `[\x00-\x08\x0b-\x1f\x7f]`.

---

## `ssh_fleet/server.py` — New Tool

### Config addition

In `_load_config()`:

```python
global _guide_output_dir
_guide_output_dir = Path(
    data.get("guide_output_dir", "~/.ssh-fleet/guides")
).expanduser()
_guide_output_dir.mkdir(parents=True, exist_ok=True)
```

### Tool

```python
@mcp_server.tool()
async def render_command(
    host: str,
    command: str,
    sudo: bool = True,
    title: Optional[str] = None,
    output_name: Optional[str] = None,
    timeout: int = 60,
) -> str:
    """Run a command on a remote host and render the output as a polished SVG terminal screenshot.

    The SVG is saved to the configured guide output directory and the absolute
    path is returned. Use this for blog posts, tutorials, and documentation —
    the output shown is whatever the command actually printed on the remote machine.

    sudo defaults to true (matches ssh-fleet's bias toward root for system commands).

    Args:
        host: Hostname or IP (case-insensitive)
        command: Shell command to execute
        sudo: Run with sudo elevation (default true)
        title: Optional title bar text; default "user@host: ~"
        output_name: Optional filename stem (no extension); default is slugified command + timestamp
        timeout: Command timeout in seconds (default 60)
    """
```

### Behavior

1. `store.get(host)` → if missing, return the exact string other tools use: `f"ERROR: Machine '{host}' not found. Use list_machines to see available machines."`
2. Run via `pool.sudo_exec(m, command, timeout)` if `sudo`, else `pool.exec(...)`. Catch `SSHConnectionError` → return `f"ERROR: {e}"`.
3. Build `RenderOptions(prompt_user=m.username, prompt_host=m.hostname, prompt_cwd="~", title=title or f"{m.username}@{m.hostname}: ~", sudo=sudo)`.
4. Call `render_terminal_svg(command, result.stdout, result.stderr, result.exit_code, opts)`.
5. Filename: `output_name` if given, else slugify first 40 chars of command + `-{int(time.time())}.svg`. Slug: lowercase, non-alphanumeric → `-`, collapse repeats, strip leading/trailing dashes.
6. Write via `Path.write_text(svg, encoding="utf-8")`. Catch `OSError` → return `f"ERROR: Cannot write to guide output directory: {path} ({e})"`.
7. Return:
   ```
   Rendered: /abs/path/to/file.svg
   [exit_code: N, K lines of output]
   ```

---

## Verification (runnable commands — actually execute these)

After implementation, run these in order. Each must succeed before moving on.

```bash
# 1. All tests pass
source .venv/bin/activate
pytest tests/ -v

# 2. Renderer is importable and produces valid SVG
python -c "
from ssh_fleet.terminal_render import render_terminal_svg, RenderOptions
svg = render_terminal_svg('ls -la', 'total 4\ndrwxr-xr-x  2 user user', '', 0, RenderOptions(prompt_user='tester', prompt_host='box-01'))
assert svg.startswith('<svg'), 'must start with <svg'
assert svg.endswith('</svg>'), 'must end with </svg>'
assert 'ls -la' in svg
assert 'tester' in svg
assert 'box-01' in svg
print('OK renderer basic')
"

# 3. HTML escaping works
python -c "
from ssh_fleet.terminal_render import render_terminal_svg, RenderOptions
svg = render_terminal_svg('echo test', '<script>alert(1)</script>', '', 0, RenderOptions())
assert '<script>' not in svg, 'unescaped script tag!'
assert '&lt;script&gt;' in svg
print('OK escape')
"

# 4. ANSI stripping works
python -c "
from ssh_fleet.terminal_render import render_terminal_svg, RenderOptions
svg = render_terminal_svg('ls', '\x1b[31mred text\x1b[0m', '', 0, RenderOptions())
assert '\x1b' not in svg
assert 'red text' in svg
print('OK ansi')
"

# 5. Truncation works
python -c "
from ssh_fleet.terminal_render import render_terminal_svg, RenderOptions
output = '\n'.join(f'line {i}' for i in range(500))
svg = render_terminal_svg('seq 500', output, '', 0, RenderOptions(max_output_lines=50))
assert 'truncated' in svg.lower()
print('OK truncate')
"

# 6. Save a sample SVG for me to eyeball
python -c "
from ssh_fleet.terminal_render import render_terminal_svg, RenderOptions
svg = render_terminal_svg(
    'docker ps --format \"table {{.Names}}\t{{.Status}}\"',
    'NAMES       STATUS\nnginx       Up 2 hours\npostgres    Up 2 hours\nredis       Up 30 minutes',
    '',
    0,
    RenderOptions(prompt_user='root', prompt_host='web-01', sudo=True)
)
open('/tmp/sample.svg', 'w').write(svg)
print('Saved /tmp/sample.svg — open in a browser')
"
```

**If any of these fail, fix before moving on.** Don't batch failures.

---

## Required Test Cases (`tests/test_terminal_render.py`)

Pure renderer tests — no SSH mocking, just literal strings in:

1. Basic success: command + simple stdout → SVG contains command and output lines
2. Failure: exit_code=1 + stderr → stderr appears in output section with `fill-opacity`
3. HTML escape: stdout with `<script>alert(1)</script>` → escaped, no raw `<script>` tag
4. ANSI strip: stdout with `\x1b[31mred\x1b[0m` → renders as `red`, no escape bytes in SVG
5. Truncation: 500-line stdout with `max_output_lines=50` → exactly 50 rendered lines + truncation marker
6. Long line wrap: 300-char line with `max_line_chars=100` → wraps into multiple text rows
7. Sizing scales: long line → wider SVG; many lines → taller SVG
8. Traffic lights present: SVG contains three `<circle>` elements with expected fills
9. Sudo visual: `sudo=True` → yellow "sudo" marker present in prompt area
10. Empty stdout + stderr + exit 0 → renders with just prompt line, no crash

### Tool integration test (`tests/test_server_render.py`)

Mock the pool like `test_pool.py` does. Assert:
- Unknown host returns the exact error string
- Successful call writes a file to the configured dir and returns a path containing `.svg`
- `SSHConnectionError` from pool → `ERROR: ...` return, no crash, no file written

---

## Checkpoint Moments

Stop and show me at each of these. Don't barrel through.

- **Checkpoint 1 (after Explore):** 3–5 bullets of what you found in the codebase. Wait for "go".
- **Checkpoint 2 (after renderer + renderer tests):** paste `/tmp/sample.svg` contents or the path, and `pytest tests/test_terminal_render.py -v` output. Wait for "go".
- **Checkpoint 3 (after tool wiring + integration tests):** diff of `server.py` and `pytest tests/ -v` output. Wait for "go".
- **Checkpoint 4 (final):** updated `CLAUDE.md` and `README.md` snippets. Then we'll pick a real host and test end-to-end.

---

## Common Failure Modes (avoid these)

- **Infinite exploration.** Don't read every file in the repo. Stick to the files listed in the Explore phase. If a file isn't listed, you probably don't need it.
- **Trust-then-verify gap.** Don't say "tests should pass" — actually run `pytest tests/ -v` and paste the output.
- **Adding a dep "just for convenience".** The answer is no. Stdlib only. If `html.escape` isn't enough, use `str.replace`.
- **Duplicating `_strip_ansi`.** It exists. Import it.
- **Using `CommandResult.format()`.** It prettifies JSON. We want raw terminal output. Read the fields directly.
- **Over-engineering the SVG.** No gradients, no animations, no JavaScript in the SVG, no embedded fonts. Flat shapes + text. That's it.
- **Scope creep into a guide assembler.** Not in v1. One tool, one SVG per call. Done.

---

## Out of Scope for v1

Put these in a `FUTURE.md` if you feel the urge. Do not implement:
- Multi-step guide assembler tool
- ANSI color → SVG color mapping
- Multiple themes
- PNG output
- Animated SVGs
- Custom fonts
- cwd tracking between calls
- Rendering for `shell_run` sessions

---

## Deliverables Checklist

- [ ] `ssh_fleet/terminal_render.py` — stdlib only
- [ ] `_strip_ansi` importable from `ssh_fleet/ssh.py`
- [ ] `tests/test_terminal_render.py` — all 10 cases above
- [ ] `tests/test_server_render.py` — 3 integration cases
- [ ] `render_command` tool in `ssh_fleet/server.py`
- [ ] `_guide_output_dir` config plumbed through `_load_config()`
- [ ] `pytest tests/ -v` green (with output pasted to me)
- [ ] Six verification snippets above all print `OK`
- [ ] Sample SVG at `/tmp/sample.svg` for me to eyeball
- [ ] `CLAUDE.md` + `README.md` updated
- [ ] `pyproject.toml` unchanged
- [ ] No regressions in existing tests

When the checklist is fully checked and all checkpoints have been shown, stop and ping me. We'll pick a real test host and run it against a live server.
