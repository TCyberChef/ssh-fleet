# ssh-fleet

Multi-machine SSH MCP server for Claude Code. One process, entire fleet.

Unlike other SSH MCP servers that require one process per host, ssh-fleet manages all your machines from a single server with connection pooling, parallel execution, and persistent shell sessions.

## Features

- **Multi-machine** - All machines in one MCP server, not one process per host
- **Connection pooling** - SSH connections stay alive across tool calls, auto-reconnect on failure
- **Parallel execution** - Run commands on multiple machines simultaneously
- **Persistent shells** - Open a root shell once, run many commands without re-authenticating sudo
- **SFTP** - Upload, download, read, and write remote files
- **Staged scripts** - Run long scripts and tmux wrappers without fragile heredocs
- **Dynamic machines** - Add machines on the fly without restarting
- **SSH key + password auth** - Supports both authentication methods

## Tools

| Tool | Description |
|------|-------------|
| `exec` | Run a command on a remote machine |
| `sudo_exec` | Run a command with sudo elevation |
| `render_command` | Run a command and render its output as an SVG/PNG terminal screenshot |
| `render_output` | Render a terminal screenshot from literal text (no SSH) |
| `render_gif` | Run a command and render an animated GIF showing output appearing line by line |
| `render_gif_output` | Render an animated GIF from literal text (no SSH) |
| `list_machines` | List all machines with metadata |
| `add_host` | Register a temporary or permanent machine |
| `reload_machines` | Re-read hosts file and refresh metadata |
| `read_file` | Read a remote file via SFTP |
| `write_file` | Write remote text content via SFTP staging |
| `upload` | Upload a local file to a remote machine |
| `download` | Download a remote file to local |
| `run_script` | Stage and run a bash script without inline heredocs |
| `tmux_new` | Start a detached remote tmux session |
| `tmux_list` | List remote tmux sessions |
| `tmux_capture` | Capture output from a remote tmux pane |
| `tmux_wait` | Wait for text or regex in a remote tmux pane |
| `tmux_paste` | Paste arbitrary text into a tmux pane via SFTP buffer |
| `tmux_send_keys` | Send tmux key tokens like `C-c` or `Enter` |
| `tmux_kill` | Kill a remote tmux session |
| `shell_open` | Open a persistent root shell |
| `shell_run` | Run a command in a persistent shell |
| `shell_close` | Close a persistent shell |
| `shell_list` | List active shell sessions |

## Installation

```bash
git clone https://github.com/YOUR_USER/ssh-fleet.git
cd ssh-fleet
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Register with Claude Code:

```bash
claude mcp add --scope user --transport stdio ssh-fleet -- /path/to/ssh-fleet/.venv/bin/ssh-fleet
```

## Configuration

Create `~/.ssh-fleet/config.yaml`:

```yaml
# Path to hosts file (tab-separated: hostname<TAB>IP<TAB>username:password)
auth_file: ~/hosts.tsv

# Optional: URL to a JSON status endpoint for machine metadata
# status_url: https://your-dashboard/status.json
```

Or set the `SSH_FLEET_CONFIG` environment variable to point to your config file.

### Hosts file format

```
# hostname<TAB>IP<TAB>username:password
web-1	192.168.1.10	admin:secretpass
db-1	192.168.1.20	postgres:dbpass123
staging	10.0.1.5	deploy:deploykey
```

Lines starting with `#` are comments. Fields are tab-separated.

### SSH key authentication

For machines using SSH keys, you can add them dynamically:

```
> add a machine called aws-prod at 10.0.1.100, user ec2-user, key file ~/.ssh/aws.pem
```

Claude Code calls:
```
add_host(hostname="aws-prod", ip="10.0.1.100", username="ec2-user", key_file="~/.ssh/aws.pem")
```

## Usage

Once registered, Claude Code can use ssh-fleet tools directly:

### Run commands
```
> check disk usage on web-1
# Claude calls: exec(host="web-1", command="df -h")

> run kubectl get pods on db-1 with sudo
# Claude calls: sudo_exec(host="db-1", command="kubectl get pods -A")
```

### Parallel execution
```
> check uptime on web-1 and db-1 at the same time
# Claude calls exec() on both machines in parallel
```

### Persistent shell (for multi-command sessions)
```
> open a shell on web-1
# Claude calls: shell_open(host="web-1")
# Returns: session ID "shell-web-1-1"

> list pods, then describe the failing one
# Claude calls: shell_run(session_id="shell-web-1-1", command="kubectl get pods")
# Claude calls: shell_run(session_id="shell-web-1-1", command="kubectl describe pod xyz")
# No sudo re-authentication between commands
```

### File operations
```
> read /etc/os-release on staging
# Claude calls: read_file(host="staging", remote_path="/etc/os-release")

> write a root-owned config file on web-1
# Claude calls: write_file(host="web-1", remote_path="/etc/example.conf",
#                          content="KEY=value\n", sudo=True, mode="0640",
#                          owner="root:root")

> upload ./fix.sh to /tmp/fix.sh on web-1
# Claude calls: upload(host="web-1", local_path="./fix.sh", remote_path="/tmp/fix.sh")
```

### Staged scripts

Use `run_script` for heredocs, installers, tmux launch wrappers, and anything
with complex quoting. The script body is uploaded through SFTP, then ssh-fleet
runs a short command such as `bash /tmp/ssh-fleet-runner-...`.

```
> run this installer wrapper on alfa in tmux and log it to /tmp/wizinst.log
# Claude calls: run_script(
#   host="alfa",
#   script="bash /tmp/install.sh ...",
#   sudo=True,
#   tmux_session="wizinst",
#   log_path="/tmp/wizinst.log",
#   env={"WIZ_SSH_PASS": "..."},
# )
```

For secrets, prefer passing them in `env` and keep `keep_script=False` so the
temporary runner is removed when it exits:

```
# Claude calls: run_script(host="alfa", script="bash /tmp/install.sh ...",
#                          env={"WIZ_SSH_PASS": "..."})
```

### Remote tmux control

Use the tmux tools when an agent needs a durable remote terminal instead of a
single command. This is the closest ssh-fleet gives Claude Code or Codex to a
native remote CLI session.

```
> start a root tmux terminal on alfa
# Claude calls: tmux_new(host="alfa", session="work_1", command="bash")

> run a command in that tmux terminal
# Claude calls: tmux_paste(host="alfa", target="work_1",
#                          text="kubectl get pods -A\n")

> show me what happened
# Claude calls: tmux_capture(host="alfa", target="work_1", lines=120)

> wait until the pod table appears
# Claude calls: tmux_wait(host="alfa", target="work_1", pattern="NAME", timeout=30)

> interrupt it
# Claude calls: tmux_send_keys(host="alfa", target="work_1", keys="C-c")

> close the session
# Claude calls: tmux_kill(host="alfa", target="work_1")
```

`tmux_paste` stages text through SFTP before loading it into a tmux buffer, so
quotes, dollar signs, heredocs, and multiline commands are not embedded in the
SSH command string. Use `tmux_send_keys` only for control keys.

### Guide rendering

Turn a remote command into a polished PNG terminal screenshot for blog posts, tutorials, or Markdown guides:

```
> render kubectl get nodes -o wide on nissan for the k3s deployment guide
# Claude calls: render_command(host="nissan", command="kubectl get nodes -o wide")
# Returns:
#   Rendered: /Users/you/.ssh-fleet/guides/kubectl-get-nodes-o-wide-1712743234.png
#   [exit_code: 0, 2 lines of output]
```

The image is styled with Catppuccin Mocha colors, macOS-style window chrome, drop shadow, and a colored prompt line showing the real `user@host` from your hosts file. Drop it straight into a blog post, MDX file, or Confluence page.

- Output directory is configurable via `guide_output_dir` in `~/.ssh-fleet/config.yaml` (default: `~/.ssh-fleet/guides`).
- Override the filename with `output_name="my-example"` instead of the auto-slugified command + timestamp.
- Requires `rsvg-convert` for SVG-to-PNG conversion (`brew install librsvg`). The SVG is a tempfile intermediate and never lands in the guide directory.
- **Tip:** for guides that highlight errors in red, pass `sudo=False`. The default `sudo=True` path merges stderr into stdout at the PTY level (needed for sudo password handling), so the red-stderr rendering only triggers when running without sudo elevation.

### Animated GIF rendering

Turn remote command output into animated GIFs that show commands being typed and output appearing line by line:

```
> render a GIF of kubectl get pods on nissan for the deployment guide
# Claude calls: render_gif(host="nissan", command="kubectl get pods")
# Returns:
#   Rendered: /Users/you/.ssh-fleet/guides/kubectl-get-pods-1712743234.gif
#   [exit_code: 0, 5 lines of output, 6 frames]
```

For pre-captured or idealized output, use `render_gif_output` (no SSH required):

```
> render a GIF showing systemctl restart output
# Claude calls: render_gif_output(command="systemctl restart onwatch", stdout="...")
```

Animation parameters:
- `line_delay_ms` - time between output lines appearing (default 200ms)
- `hold_ms` - time to hold the final frame (default 3000ms)
- `batch_lines` - lines to reveal per frame (default 1, use 3+ for long output)
- `max_output_lines` - truncation limit (default 50, since each line = a frame)

**Requirements:** `brew install librsvg ffmpeg`

### Dynamic machines
```
> add a temporary machine called test-box at 192.168.5.10, user admin, password secret
# Claude calls: add_host(hostname="test-box", ip="192.168.5.10", username="admin", password="secret")
# Machine is available immediately, gone when session ends
```

## Architecture

```
Claude Code
    |
    v
ssh-fleet MCP server (single Python process)
    |
    +-- Connection Pool
    |       +-- web-1: paramiko.SSHClient (persistent, idle timeout 5min)
    |       +-- db-1: paramiko.SSHClient
    |       +-- staging: paramiko.SSHClient
    |
    +-- Shell Sessions
    |       +-- shell-web-1-1: SudoShell (persistent root shell, idle timeout 30min)
    |
    +-- Machine Store
            +-- hosts file (loaded at startup)
            +-- dashboard metadata (optional, loaded at startup)
            +-- temporary machines (session-only, in-memory)
```

## Optional: Dashboard Integration

If you have a status dashboard that exposes machine metadata as JSON, configure `status_url` in your config. The expected format:

**status.json** (keyed by IP):
```json
{
  "192.168.1.10": {
    "hostname": "web-1",
    "version": "2.1.0",
    "os_version": "Ubuntu 22.04",
    "machine_state": "active"
  }
}
```

**owners.json** (optional, keyed by IP):
```json
{
  "192.168.1.10": "alice",
  "192.168.1.20": {"username": "bob", "description": "database testing"}
}
```

This enriches `list_machines` output with version, OS, state, and ownership info.

## Development

```bash
# Run tests
pip install -e ".[test]"
pytest tests/ -v

# Run the server directly
ssh-fleet
```

## License

MIT
