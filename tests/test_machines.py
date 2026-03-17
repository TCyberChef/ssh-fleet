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


def test_store_list_all_includes_ssh_info():
    store = MachineStore()
    store.load_from_string("web-1\t192.168.1.10\tadmin:pass\n")
    all_machines = store.list_all()
    assert len(all_machines) == 1
    assert all_machines[0]["ssh"] is True
    assert all_machines[0]["temporary"] is False
