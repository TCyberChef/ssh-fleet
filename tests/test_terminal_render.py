"""Tests for terminal SVG renderer.

Pure tests — no SSH, no MCP. Literal strings in, SVG string out.
"""
import re

from ssh_fleet.terminal_render import render_terminal_svg, RenderOptions


def test_basic_success_contains_command_and_output():
    """Command + stdout → SVG contains both, plus user@host in prompt."""
    svg = render_terminal_svg(
        "ls -la",
        "total 4\ndrwxr-xr-x 2 user user",
        "",
        0,
        RenderOptions(prompt_user="tester", prompt_host="box-01"),
    )
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "ls -la" in svg
    assert "total 4" in svg
    assert "drwxr-xr-x" in svg
    assert "tester" in svg
    assert "box-01" in svg


def test_failure_stderr_has_fill_opacity():
    """Non-zero exit + stderr → stderr rendered with fill-opacity="0.85"."""
    svg = render_terminal_svg(
        "cat /missing",
        "",
        "cat: /missing: No such file or directory",
        1,
        RenderOptions(),
    )
    assert "No such file or directory" in svg
    assert 'fill-opacity="0.85"' in svg


def test_html_escape_prevents_injection():
    """Angle brackets and script tags in stdout must be HTML-escaped."""
    svg = render_terminal_svg(
        "echo test",
        "<script>alert(1)</script>",
        "",
        0,
        RenderOptions(),
    )
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert "alert(1)" in svg


def test_ansi_escape_codes_stripped():
    """ANSI color escape sequences must be removed before rendering."""
    svg = render_terminal_svg(
        "ls",
        "\x1b[31mred text\x1b[0m",
        "",
        0,
        RenderOptions(),
    )
    assert "\x1b" not in svg
    assert "red text" in svg


def test_truncation_at_max_output_lines():
    """500-line stdout with max_output_lines=50 → truncated with marker."""
    output = "\n".join(f"line {i}" for i in range(500))
    svg = render_terminal_svg(
        "seq 500",
        output,
        "",
        0,
        RenderOptions(max_output_lines=50),
    )
    assert "truncated" in svg.lower()
    # Spec: keep first (max - 1) → lines 0..48 visible, lines 49..499 dropped.
    assert ">line 0<" in svg
    assert ">line 48<" in svg
    assert ">line 100<" not in svg
    assert ">line 499<" not in svg


def test_long_line_wrap_creates_multiple_rows():
    """300-char line with max_line_chars=100 → 3 rows of 100 chars each."""
    long_line = "x" * 300
    svg = render_terminal_svg(
        "echo long",
        long_line,
        "",
        0,
        RenderOptions(max_line_chars=100),
    )
    # Each wrap chunk is its own <text> element with nothing but x's.
    x_rows = re.findall(r'<text[^>]*>x+</text>', svg)
    assert len(x_rows) >= 3


def test_sizing_scales_with_content():
    """Wider content → wider SVG; taller content → taller SVG."""
    short_svg = render_terminal_svg(
        "echo", "short", "", 0, RenderOptions()
    )
    wide_svg = render_terminal_svg(
        "echo", "x" * 130, "", 0, RenderOptions()
    )
    tall_svg = render_terminal_svg(
        "echo",
        "\n".join(f"l{i}" for i in range(50)),
        "",
        0,
        RenderOptions(),
    )

    def dims(s):
        w = int(re.search(r'<svg[^>]*\bwidth="(\d+)"', s).group(1))
        h = int(re.search(r'<svg[^>]*\bheight="(\d+)"', s).group(1))
        return w, h

    sw, sh = dims(short_svg)
    ww, _ = dims(wide_svg)
    _, th = dims(tall_svg)

    assert ww > sw, f"wide ({ww}) should exceed short ({sw})"
    assert th > sh, f"tall ({th}) should exceed short ({sh})"


def test_traffic_lights_present():
    """Window chrome has three traffic light circles in the spec-mandated colors."""
    svg = render_terminal_svg("ls", "hi", "", 0, RenderOptions())
    assert "#ff5f57" in svg
    assert "#febc2e" in svg
    assert "#28c840" in svg
    circles = re.findall(r'<circle[^/]*/>', svg)
    assert len(circles) >= 3


def test_sudo_visual_shows_yellow_marker():
    """sudo=True → yellow 'sudo' marker appears in the prompt area."""
    svg = render_terminal_svg(
        "systemctl status",
        "active",
        "",
        0,
        RenderOptions(sudo=True),
    )
    # Catppuccin yellow for the sudo marker
    assert "#f9e2af" in svg
    # The literal word is rendered somewhere
    assert "sudo" in svg.lower()


def test_empty_output_no_crash():
    """Empty stdout + stderr + exit 0 → valid SVG with just the prompt line."""
    svg = render_terminal_svg("true", "", "", 0, RenderOptions())
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "true" in svg


def test_dec_keypad_escapes_stripped_cleanly():
    """Real pty output from `systemctl status` wraps content in \\x1b= / \\x1b>.

    ssh.py's _strip_ansi handles the CSI sequences (\\x1b[?1h etc.) but not the
    bare C1 controls \\x1b= (DECKPAM) and \\x1b> (DECKPNM). Those must not leak
    into the rendered SVG as stray '=' and '>' characters prefixing the content.
    """
    raw = (
        "\x1b[?1h\x1b=\rUnit foo.service could not be found."
        "\x1b[m\r\n\r\x1b[K\x1b[?1l\x1b>"
    )
    svg = render_terminal_svg("systemctl status foo", raw, "", 4, RenderOptions())

    # No raw escape byte
    assert "\x1b" not in svg
    # The real content is there
    assert "Unit foo.service could not be found." in svg
    # No row with "=Unit..." (bug: \x1b= leaked as literal '=')
    assert "=Unit" not in svg
    # No row that is just ">" (bug: \x1b> leaked as literal '>')
    weird = re.findall(r"<text[^>]*>&gt;</text>|<text[^>]*>></text>", svg)
    assert not weird, f"stray '>' rows: {weird}"
