"""Tests for machine loading and resolution."""
import pytest
from ssh_fleet.machines import Machine, MachineStore


def test_parse_machine_line():
    m = Machine.from_line("Ferrari\t10.1.25.5\tuser:pass123")
    assert m.hostname == "Ferrari"
    assert m.ip == "10.1.25.5"
    assert m.username == "user"
    assert m.password == "pass123"


def test_parse_machine_line_special_password():
    m = Machine.from_line("Azul\t10.1.71.1\tuser:user1!")
    assert m.password == "user1!"


def test_parse_machine_line_comment():
    assert Machine.from_line("# this is a comment") is None


def test_parse_machine_line_empty():
    assert Machine.from_line("") is None
    assert Machine.from_line("   ") is None


def test_parse_machine_line_malformed():
    assert Machine.from_line("only-one-field") is None
    assert Machine.from_line("two\tfields") is None


def test_store_load_from_string():
    content = "Ferrari\t10.1.25.5\tuser:pass\nAzul\t10.1.71.1\tuser:pass2\n# comment\n"
    store = MachineStore()
    store.load_from_string(content)
    assert len(store.list_ssh_machines()) == 2


def test_store_case_insensitive_lookup():
    store = MachineStore()
    store.load_from_string("Ferrari\t10.1.25.5\tuser:pass\n")
    assert store.get("ferrari") is not None
    assert store.get("FERRARI") is not None
    assert store.get("Ferrari") is not None


def test_store_add_temporary():
    store = MachineStore()
    store.add_temporary("NewVM", "10.1.50.10", "admin", "secret")
    m = store.get("newvm")
    assert m is not None
    assert m.hostname == "NewVM"
    assert m.temporary is True


def test_store_add_temporary_duplicate_rejected():
    store = MachineStore()
    store.load_from_string("Ferrari\t10.1.25.5\tuser:pass\n")
    with pytest.raises(ValueError, match="already exists"):
        store.add_temporary("ferrari", "10.1.50.10", "admin", "secret")


def test_store_reload_preserves_temporary():
    store = MachineStore()
    store.load_from_string("Ferrari\t10.1.25.5\tuser:pass\n")
    store.add_temporary("NewVM", "10.1.50.10", "admin", "secret")
    store.load_from_string("Ferrari\t10.1.25.5\tuser:pass\nAzul\t10.1.71.1\tuser:pass2\n")
    assert store.get("newvm") is not None
    assert store.get("azul") is not None


def test_store_unknown_machine():
    store = MachineStore()
    store.load_from_string("Ferrari\t10.1.25.5\tuser:pass\n")
    assert store.get("nonexistent") is None


def test_store_list_all_includes_ssh_info():
    store = MachineStore()
    store.load_from_string("Ferrari\t10.1.25.5\tuser:pass\n")
    all_machines = store.list_all()
    assert len(all_machines) == 1
    assert all_machines[0]["ssh"] is True
    assert all_machines[0]["temporary"] is False
