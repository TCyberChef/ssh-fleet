"""Terminal SVG renderer - stdlib only.

Pure data in -> SVG string out. No SSH, no MCP, no async.
Renders a polished macOS-style terminal screenshot with Catppuccin Mocha
styling. Trivially unit-testable.
"""
import html
import re
from dataclasses import dataclass
from typing import Optional

from ssh_fleet.ssh import _strip_ansi

# --- Catppuccin Mocha palette ---
BASE = "#1e1e2e"       # window background
TEXT = "#cdd6f4"       # default foreground
SURFACE0 = "#313244"   # title bar background
GREEN = "#a6e3a1"      # user@host
BLUE = "#89b4fa"       # cwd
RED = "#f38ba8"        # stderr / error
YELLOW = "#f9e2af"     # sudo marker
OVERLAY0 = "#6c7086"   # muted / truncation notice

# Title bar text color (Catppuccin subtext1)
TITLE_FG = "#9399b2"

# --- macOS traffic light colors (spec-mandated, NOT Catppuccin) ---
TRAFFIC_RED = "#ff5f57"
TRAFFIC_YELLOW = "#febc2e"
TRAFFIC_GREEN = "#28c840"

# --- Typography ---
FONT_FAMILY = (
    "ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Consolas, monospace"
)

# --- Chrome dimensions ---
TITLE_BAR_HEIGHT = 28
PADDING = 20
CORNER_RADIUS = 10

# --- Sizing bounds ---
MIN_WIDTH = 600
MAX_WIDTH = 1400

# Control characters we strip (keeps \t=0x09 and \n=0x0a).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Bare C1 escape sequences that ssh.py's _strip_ansi doesn't cover.
# Without this, `systemctl status` (which sets DEC keypad modes via pty) leaks
# literal '=' and '>' characters into the rendered output. Covers:
#   - \x1b= (DECKPAM), \x1b> (DECKPNM), \x1b7 (DECSC), \x1b8 (DECRC),
#     \x1bH (HTS), \x1bM (RI), \x1bc (RIS)
#   - Charset selection: \x1b(B (G0=ASCII), \x1b)0 (G1=graphics), etc.
_EXTRA_ESC_RE = re.compile(
    r"\x1b[()][0-9A-Za-z]"    # charset selection (3-char sequence)
    r"|\x1b[=><78HMc]"         # bare single-char C1 controls
)


@dataclass
class RenderOptions:
    """Rendering knobs for the terminal SVG."""
    title: Optional[str] = None            # defaults to "user@host: ~"
    prompt_user: str = "user"
    prompt_host: str = "host"
    prompt_cwd: str = "~"
    max_output_lines: int = 200
    max_line_chars: int = 140
    font_size: int = 14
    sudo: bool = False                     # visual "sudo " prefix in yellow
    min_height: Optional[int] = None       # ensure SVG is at least this tall (for animation frames)


def _clean(text: str) -> str:
    """Strip ANSI escape sequences, carriage returns, and non-printable control chars.

    Runs ssh.py's _strip_ansi first (CSI/OSC), then our supplementary regex for
    bare C1 controls and charset selection that _strip_ansi misses, then strips
    \\r and any remaining non-printable control bytes.
    """
    text = _strip_ansi(text)
    text = _EXTRA_ESC_RE.sub("", text)
    text = text.replace("\r", "")
    text = _CONTROL_RE.sub("", text)
    return text


def _split_lines(text: str) -> list:
    """Clean and split text into lines, dropping trailing empties."""
    if not text:
        return []
    lines = _clean(text).split("\n")
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _wrap_line(line: str, max_chars: int) -> list:
    """Soft-wrap a line into fixed-width chunks. Never returns an empty list."""
    if len(line) <= max_chars:
        return [line]
    return [line[i:i + max_chars] for i in range(0, len(line), max_chars)]


def _esc(text: str) -> str:
    """Escape user content for embedding in SVG <text> / <tspan> elements."""
    return html.escape(text, quote=False)


def render_terminal_svg(
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    opts: RenderOptions,
) -> str:
    """Render a terminal session as a self-contained SVG document.

    The SVG has no external dependencies (no web fonts, no JS, no CSS imports).
    """
    font_size = opts.font_size
    line_height = font_size * 1.5
    char_width = font_size * 0.6

    # --- Prompt line (single logical row, may wrap if very long) ---
    # Newlines in the command itself would confuse the prompt renderer; flatten them.
    command_clean = _clean(command).replace("\n", " ")
    sudo_prefix = "sudo " if opts.sudo else ""
    prompt_plain = (
        f"{opts.prompt_user}@{opts.prompt_host}"
        f":{opts.prompt_cwd}$ {sudo_prefix}{command_clean}"
    )
    prompt_wrapped = _wrap_line(prompt_plain, opts.max_line_chars)

    # --- Output rows: (text, fill_color, fill_opacity_or_none) ---
    output_rows = []
    for line in _split_lines(stdout):
        for chunk in _wrap_line(line, opts.max_line_chars):
            output_rows.append((chunk, TEXT, None))
    for line in _split_lines(stderr):
        for chunk in _wrap_line(line, opts.max_line_chars):
            output_rows.append((chunk, RED, "0.85"))

    # --- Truncate overly long output ---
    if len(output_rows) > opts.max_output_lines:
        keep = max(0, opts.max_output_lines - 1)
        n_truncated = len(output_rows) - keep
        output_rows = output_rows[:keep]
        output_rows.append((
            f"... ({n_truncated} lines truncated)",
            OVERLAY0,
            None,
        ))

    # --- Sizing ---
    char_counts = [len(r) for r in prompt_wrapped]
    char_counts.extend(len(t) for t, _, _ in output_rows)
    max_chars = max(char_counts) if char_counts else 0

    raw_width = max_chars * char_width + 2 * PADDING
    width = int(max(MIN_WIDTH, min(MAX_WIDTH, raw_width)))

    total_rows = max(len(prompt_wrapped) + len(output_rows), 1)
    # Content height: first row's ascent (font_size) + pitch between the
    # remaining rows (line_height each). The last row does NOT reserve an
    # inter-line gap below itself — that's what produced visible dead space.
    height = int(
        TITLE_BAR_HEIGHT + PADDING
        + font_size + (total_rows - 1) * line_height
        + PADDING
    )
    if opts.min_height:
        height = max(height, opts.min_height)

    # --- Assemble SVG ---
    title = opts.title or f"{opts.prompt_user}@{opts.prompt_host}: ~"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" '
        f'font-family="{FONT_FAMILY}" font-size="{font_size}">',
        # Drop shadow filter for the window
        '<defs>'
        '<filter id="win-shadow" x="-20%" y="-20%" width="140%" height="140%">'
        '<feGaussianBlur in="SourceAlpha" stdDeviation="4"/>'
        '<feOffset dx="0" dy="4" result="offsetblur"/>'
        '<feComponentTransfer><feFuncA type="linear" slope="0.4"/></feComponentTransfer>'
        '<feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>'
        '</filter>'
        '</defs>',
        # Window background with rounded corners + drop shadow
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'rx="{CORNER_RADIUS}" ry="{CORNER_RADIUS}" '
        f'fill="{BASE}" filter="url(#win-shadow)"/>',
        # Title bar: rounded top corners, flat bottom
        f'<path d="M 0 {CORNER_RADIUS} '
        f'a {CORNER_RADIUS} {CORNER_RADIUS} 0 0 1 {CORNER_RADIUS} -{CORNER_RADIUS} '
        f'L {width - CORNER_RADIUS} 0 '
        f'a {CORNER_RADIUS} {CORNER_RADIUS} 0 0 1 {CORNER_RADIUS} {CORNER_RADIUS} '
        f'L {width} {TITLE_BAR_HEIGHT} L 0 {TITLE_BAR_HEIGHT} Z" '
        f'fill="{SURFACE0}"/>',
        # Traffic lights (centers at x=16,36,56; cy=14)
        f'<circle cx="16" cy="14" r="6" fill="{TRAFFIC_RED}"/>',
        f'<circle cx="36" cy="14" r="6" fill="{TRAFFIC_YELLOW}"/>',
        f'<circle cx="56" cy="14" r="6" fill="{TRAFFIC_GREEN}"/>',
        # Title text centered in title bar
        f'<text x="{width // 2}" y="18" text-anchor="middle" '
        f'fill="{TITLE_FG}" font-size="12">{_esc(title)}</text>',
    ]

    # --- Content rows ---
    # Baseline offset: place first row's baseline at PADDING + font_size under the title bar.
    y0 = TITLE_BAR_HEIGHT + PADDING + font_size
    row_index = 0

    if len(prompt_wrapped) == 1:
        # Render prompt with colored tspans
        y = y0 + row_index * line_height
        parts.append(_render_colored_prompt(PADDING, y, opts, command_clean))
        row_index += 1
    else:
        # Very long prompt: fall back to plain-text rendering for continuation rows
        for row in prompt_wrapped:
            y = y0 + row_index * line_height
            parts.append(
                f'<text x="{PADDING}" y="{y}" fill="{TEXT}" xml:space="preserve">'
                f'{_esc(row)}</text>'
            )
            row_index += 1

    # Output rows
    for text, color, opacity in output_rows:
        y = y0 + row_index * line_height
        opacity_attr = f' fill-opacity="{opacity}"' if opacity else ""
        parts.append(
            f'<text x="{PADDING}" y="{y}" fill="{color}"{opacity_attr} xml:space="preserve">'
            f'{_esc(text)}</text>'
        )
        row_index += 1

    parts.append("</svg>")
    return "\n".join(parts)


def _extract_height(svg: str) -> int:
    """Extract the height attribute from an SVG string."""
    m = re.search(r'<svg[^>]*\bheight="(\d+)"', svg)
    return int(m.group(1)) if m else 0


def render_terminal_frames(
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    opts: RenderOptions,
    batch_lines: int = 1,
) -> list[str]:
    """Generate progressive SVG frames for an animated terminal GIF.

    Returns a list of SVG strings, each showing progressively more output:
      - Frame 0: prompt + command, no output yet
      - Frames 1..N: progressive stdout reveal (batch_lines per frame)
      - Frames N+1..M: progressive stderr reveal after all stdout
      - Final frame: full content (same as last progressive frame)

    All frames have identical dimensions (set via min_height from the
    full-content render).
    """
    from dataclasses import replace

    # Render full content first to get the natural height
    full_svg = render_terminal_svg(command, stdout, stderr, exit_code, opts)
    target_height = _extract_height(full_svg)

    # All frames use this height
    frame_opts = replace(opts, min_height=target_height)

    stdout_lines = _split_lines(stdout)
    stderr_lines = _split_lines(stderr)

    # If no output at all, just one frame (the prompt)
    if not stdout_lines and not stderr_lines:
        return [full_svg]

    frames: list[str] = []

    # Frame 0: prompt + command, no output
    frames.append(render_terminal_svg(command, "", "", exit_code, frame_opts))

    # Progressive stdout reveal
    for i in range(batch_lines, len(stdout_lines) + 1, batch_lines):
        partial_stdout = "\n".join(stdout_lines[:i])
        frames.append(render_terminal_svg(
            command, partial_stdout, "", exit_code, frame_opts
        ))

    # If batch_lines didn't land exactly on the last stdout line, add it
    if stdout_lines and len(stdout_lines) % batch_lines != 0:
        frames.append(render_terminal_svg(
            command, "\n".join(stdout_lines), "", exit_code, frame_opts
        ))

    # Progressive stderr reveal (after all stdout)
    full_stdout = "\n".join(stdout_lines) if stdout_lines else ""
    for i in range(batch_lines, len(stderr_lines) + 1, batch_lines):
        partial_stderr = "\n".join(stderr_lines[:i])
        frames.append(render_terminal_svg(
            command, full_stdout, partial_stderr, exit_code, frame_opts
        ))

    # If batch_lines didn't land exactly on the last stderr line, add it
    if stderr_lines and len(stderr_lines) % batch_lines != 0:
        frames.append(render_terminal_svg(
            command, full_stdout, "\n".join(stderr_lines), exit_code, frame_opts
        ))

    return frames


def _render_colored_prompt(x: int, y: float, opts: RenderOptions, command_clean: str) -> str:
    """Render a single-line prompt with colored tspans.

    Layout: user@host (green) : (text) cwd (blue) $ (text) [sudo (yellow) ] command (text)
    """
    userhost = f"{opts.prompt_user}@{opts.prompt_host}"
    segments = [
        f'<text x="{x}" y="{y}" xml:space="preserve">',
        f'<tspan fill="{GREEN}">{_esc(userhost)}</tspan>',
        f'<tspan fill="{TEXT}">:</tspan>',
        f'<tspan fill="{BLUE}">{_esc(opts.prompt_cwd)}</tspan>',
        f'<tspan fill="{TEXT}">$ </tspan>',
    ]
    if opts.sudo:
        segments.append(f'<tspan fill="{YELLOW}">sudo </tspan>')
    segments.append(f'<tspan fill="{TEXT}">{_esc(command_clean)}</tspan>')
    segments.append("</text>")
    return "".join(segments)
