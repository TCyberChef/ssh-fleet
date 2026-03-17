# ssh-fleet

Multi-machine SSH MCP server for Claude Code. One process, entire fleet.

Unlike other SSH MCP servers that require one process per host, ssh-fleet manages all your machines from a single server with connection pooling, parallel execution, and persistent shell sessions.

## Features

- **Multi-machine** - All machines in one MCP server, not one process per host
- **Connection pooling** - SSH connections stay alive across tool calls, auto-reconnect on failure
- **Parallel execution** - Run commands on multiple machines simultaneously
- **Persistent shells** - Open a root shell once, run many commands without re-authenticating sudo
- **SFTP** - Upload, download, and read remote files
- **Dynamic machines** - Add machines on the fly without restarting
- **SSH key + password auth** - Supports both authentication methods

## Tools

| Tool | Description |
|------|-------------|
| `exec` | Run a command on a remote machine |
| `sudo_exec` | Run a command with sudo elevation |
| `list_machines` | List all machines with metadata |
| `add_machine` | Register a temporary machine (session-only) |
| `reload_machines` | Re-read hosts file and refresh metadata |
| `read_file` | Read a remote file via SFTP |
| `upload` | Upload a local file to a remote machine |
| `download` | Download a remote file to local |
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
add_machine(hostname="aws-prod", ip="10.0.1.100", username="ec2-user", key_file="~/.ssh/aws.pem")
```

## Usage

Once registered, Claude Code can use ssh-fleet tools directly:

### Run commands
```
> check disk usage on web-1
# Claude calls: exec(machine="web-1", command="df -h")

> run kubectl get pods on db-1 with sudo
# Claude calls: sudo_exec(machine="db-1", command="kubectl get pods -A")
```

### Parallel execution
```
> check uptime on web-1 and db-1 at the same time
# Claude calls exec() on both machines in parallel
```

### Persistent shell (for multi-command sessions)
```
> open a shell on web-1
# Claude calls: shell_open(machine="web-1")
# Returns: session ID "shell-web-1-1"

> list pods, then describe the failing one
# Claude calls: shell_run(session_id="shell-web-1-1", command="kubectl get pods")
# Claude calls: shell_run(session_id="shell-web-1-1", command="kubectl describe pod xyz")
# No sudo re-authentication between commands
```

### File operations
```
> read /etc/os-release on staging
# Claude calls: read_file(machine="staging", remote_path="/etc/os-release")

> upload ./fix.sh to /tmp/fix.sh on web-1
# Claude calls: upload(machine="web-1", local_path="./fix.sh", remote_path="/tmp/fix.sh")
```

### Dynamic machines
```
> add a temporary machine called test-box at 192.168.5.10, user admin, password secret
# Claude calls: add_machine(hostname="test-box", ip="192.168.5.10", username="admin", password="secret")
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
