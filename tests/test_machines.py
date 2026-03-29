"""Tests for machine loading and resolution."""
import pytest
from ssh_fleet.machines import Machine, MachineStore


def test_parse_machine_line():
    m = Machine.from_line("web-1\t192.168.1.10\tadmin:secret123")
    assert m.hostname == "web-1"
    assert m.ip == "192.168.1.10"
    assert m.username == "admin"
    assert m.password == "secret123"


def test_parse_machine_line_special_password():
    m = Machine.from_line("db-1\t192.168.1.20\tadmin:p@ss!word")
    assert m.password == "p@ss!word"


def test_parse_machine_line_comment():
    assert Machine.from_line("# this is a comment") is None


def test_parse_machine_line_empty():
    assert Machine.from_line("") is None
    assert Machine.from_line("   ") is None


def test_parse_machine_line_malformed():
    assert Machine.from_line("only-one-field") is None
    assert Machine.from_line("two\tfields") is None


def test_store_load_from_string():
    content = "web-1\t192.168.1.10\tadmin:pass\ndb-1\t192.168.1.20\tadmin:pass2\n# comment\n"
    store = MachineStore()
    store.load_from_string(content)
    assert len(store.list_ssh_machines()) == 2


def test_store_case_insensitive_lookup():
    store = MachineStore()
    store.load_from_string("Web-1\t192.168.1.10\tadmin:pass\n")
    assert store.get("web-1") is not None
    assert store.get("WEB-1") is not None
    assert store.get("Web-1") is not None


def test_store_add_temporary():
    store = MachineStore()
    store.add_temporary("staging", "192.168.2.10", "deploy", "secret")
    m = store.get("staging")
    assert m is not None
    assert m.hostname == "staging"
    assert m.temporary is True


def test_store_add_temporary_with_key():
    store = MachineStore()
    store.add_temporary("aws-box", "10.0.1.5", "ec2-user", key_file="~/.ssh/id_rsa")
    m = store.get("aws-box")
    assert m is not None
    assert m.key_file == "~/.ssh/id_rsa"
    assert m.password == ""


def test_store_add_temporary_duplicate_rejected():
    store = MachineStore()
    store.load_from_string("web-1\t192.168.1.10\tadmin:pass\n")
    with pytest.raises(ValueError, match="already exists"):
        store.add_temporary("web-1", "192.168.2.10", "admin", "secret")


def test_store_reload_preserves_temporary():
    store = MachineStore()
    store.load_from_string("web-1\t192.168.1.10\tadmin:pass\n")
    store.add_temporary("staging", "192.168.2.10", "deploy", "secret")
    store.load_from_string("web-1\t192.168.1.10\tadmin:pass\ndb-1\t192.168.1.20\tadmin:pass2\n")
    assert store.get("staging") is not None
    assert store.get("db-1") is not None


def test_store_unknown_machine():
    store = MachineStore()
    store.load_from_string("web-1\t192.168.1.10\tadmin:pass\n")
    assert store.get("nonexistent") is None


def test_store_lookup_by_ip():
    store = MachineStore()
    store.load_from_string("web-1\t192.168.1.10\tadmin:pass\n")
    m = store.get("192.168.1.10")
    assert m is not None
    assert m.hostname == "web-1"


def test_store_lookup_by_ip_temporary():
    store = MachineStore()
    store.add_temporary("staging", "10.0.0.5", "deploy", "secret")
    m = store.get("10.0.0.5")
    assert m is not None
    assert m.hostname == "staging"
    assert m.temporary is True


def test_store_lookup_by_ip_unknown():
    store = MachineStore()
    store.load_from_string("web-1\t192.168.1.10\tadmin:pass\n")
    assert store.get("10.99.99.99") is None


def test_store_add_permanent(tmp_path):
    auth_file = tmp_path / "hosts.txt"
    auth_file.write_text("web-1\t192.168.1.10\tadmin:pass\n")
    store = MachineStore()
    store.load_from_file(auth_file)
    m = store.add_permanent("db-1", "192.168.1.20", "admin", password="secret", auth_file=auth_file)
    assert m.hostname == "db-1"
    assert m.temporary is False
    # Verify written to file
    content = auth_file.read_text()
    assert "db-1\t192.168.1.20\tadmin:secret" in content
    # Verify in store
    assert store.get("db-1") is not None


def test_store_add_permanent_duplicate_rejected(tmp_path):
    auth_file = tmp_path / "hosts.txt"
    auth_file.write_text("web-1\t192.168.1.10\tadmin:pass\n")
    store = MachineStore()
    store.load_from_file(auth_file)
    with pytest.raises(ValueError, match="already exists"):
        store.add_permanent("web-1", "192.168.1.20", "admin", password="x", auth_file=auth_file)


def test_store_add_permanent_no_auth_file():
    store = MachineStore()
    with pytest.raises(ValueError, match="No auth_file"):
        store.add_permanent("db-1", "192.168.1.20", "admin", password="x")


def test_store_add_permanent_no_password(tmp_path):
    auth_file = tmp_path / "hosts.txt"
    auth_file.write_text("")
    store = MachineStore()
    with pytest.raises(ValueError, match="require a password"):
        store.add_permanent("db-1", "192.168.1.20", "admin", auth_file=auth_file)


def test_store_list_all_includes_ssh_info():
    store = MachineStore()
    store.load_from_string("web-1\t192.168.1.10\tadmin:pass\n")
    all_machines = store.list_all()
    assert len(all_machines) == 1
    assert all_machines[0]["ssh"] is True
    assert all_machines[0]["temporary"] is False
