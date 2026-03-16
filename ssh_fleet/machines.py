"""Machine credential loading and resolution."""
import logging
import ssl
import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

logger = logging.getLogger("ssh-fleet")


@dataclass
class Machine:
    """SSH-accessible machine."""
    hostname: str
    ip: str
    username: str
    password: str
    temporary: bool = False
    # Dashboard metadata (optional)
    version: str = ""
    os_version: str = ""
    state: str = ""
    owner: str = ""

    @classmethod
    def from_line(cls, line: str) -> Optional["Machine"]:
        """Parse a machine from an ip.lst line.

        Format: hostname<TAB>IP<TAB>username:password
        """
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        parts = line.split("\t")
        if len(parts) < 3:
            return None
        hostname = parts[0].strip()
        ip = parts[1].strip()
        creds = parts[2].strip()
        if ":" not in creds:
            return None
        username, password = creds.split(":", 1)
        return cls(hostname=hostname, ip=ip, username=username, password=password)


class MachineStore:
    """Manages machine inventory from ip.lst, dashboard, and temp additions."""

    def __init__(self):
        self._machines: dict[str, Machine] = {}  # lowercase key -> Machine
        self._temp_machines: dict[str, Machine] = {}  # lowercase key -> Machine
        self._dashboard_meta: dict[str, dict] = {}  # ip -> metadata
        self._owners: dict[str, str] = {}  # ip -> owner string

    def load_from_file(self, path: Path) -> int:
        """Load machines from an ip.lst file."""
        content = path.read_text()
        return self.load_from_string(content)

    def load_from_string(self, content: str) -> int:
        """Load machines from ip.lst content. Preserves temporary machines."""
        self._machines.clear()
        count = 0
        for line in content.splitlines():
            m = Machine.from_line(line)
            if m:
                self._machines[m.hostname.lower()] = m
                count += 1
        return count

    def load_dashboard(self, status_url: str) -> None:
        """Fetch status.json and owners.json from dashboard. Fails silently."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # Fetch status.json
        try:
            req = urllib.request.Request(status_url)
            with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
                status = json.loads(r.read())
            self._dashboard_meta = status
            # Enrich machines with dashboard metadata
            for ip, info in status.items():
                hostname = info.get("hostname", "")
                key = hostname.lower()
                if key in self._machines:
                    self._machines[key].version = info.get("version", "")
                    self._machines[key].os_version = info.get("os_version", "")
                    self._machines[key].state = info.get("machine_state", "")
        except Exception as e:
            logger.debug(f"Dashboard status fetch failed: {e}")

        # Fetch owners.json
        owners_url = status_url.replace("/status.json", "/owners.json")
        try:
            req = urllib.request.Request(owners_url)
            with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
                owners = json.loads(r.read())
            for ip, owner_val in owners.items():
                if isinstance(owner_val, dict):
                    self._owners[ip] = owner_val.get("username", "")
                else:
                    self._owners[ip] = str(owner_val) if owner_val else ""
        except Exception as e:
            logger.debug(f"Dashboard owners fetch failed: {e}")

    def get(self, hostname: str) -> Optional[Machine]:
        """Look up a machine by hostname (case-insensitive)."""
        key = hostname.lower()
        return self._temp_machines.get(key) or self._machines.get(key)

    def add_temporary(self, hostname: str, ip: str, username: str, password: str) -> Machine:
        """Add a temporary machine for this session."""
        key = hostname.lower()
        if key in self._machines or key in self._temp_machines:
            raise ValueError(f"Machine '{hostname}' already exists")
        m = Machine(
            hostname=hostname, ip=ip, username=username,
            password=password, temporary=True
        )
        self._temp_machines[key] = m
        return m

    def list_ssh_machines(self) -> list[Machine]:
        """List all machines with SSH credentials."""
        machines = list(self._machines.values()) + list(self._temp_machines.values())
        return sorted(machines, key=lambda m: m.hostname.lower())

    def list_all(self) -> list[dict]:
        """List all machines (SSH + dashboard-only) for display."""
        result = []
        seen_ips = set()

        # SSH-capable machines first
        for m in self.list_ssh_machines():
            owner = self._owners.get(m.ip, "")
            result.append({
                "hostname": m.hostname,
                "ip": m.ip,
                "version": m.version or "-",
                "os": m.os_version or "-",
                "state": m.state or "-",
                "ssh": True,
                "temporary": m.temporary,
                "owner": owner,
            })
            seen_ips.add(m.ip)

        # Dashboard-only machines
        for ip, info in self._dashboard_meta.items():
            if ip not in seen_ips:
                hostname = info.get("hostname", ip)
                owner = self._owners.get(ip, "")
                result.append({
                    "hostname": hostname,
                    "ip": ip,
                    "version": info.get("version", "-"),
                    "os": info.get("os_version", "-"),
                    "state": info.get("machine_state", "-"),
                    "ssh": False,
                    "temporary": False,
                    "owner": owner,
                })

        return sorted(result, key=lambda m: m["hostname"].lower())
