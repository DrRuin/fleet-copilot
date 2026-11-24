"""The evidence rules for actions, and the audit log they write to."""

import sqlite3

import pytest

from app.actions import CreateUpgradeOrder, FlagDeviceForReplacement, NotifyEmployee, OpenRemediationTicket, propose
from app.tools import DetectPatterns, FindDevices, detect_patterns, find_devices


def evidence(tenant, pattern: str) -> dict:
    """Rows of one detector keyed by their refs."""
    rows = detect_patterns(tenant, DetectPatterns(pattern=pattern))
    for i, r in enumerate(rows):
        r.ref = f"R{i + 1}"
    return {r.ref: r for r in rows}


def ref_for(refs: dict, device: str) -> list[str]:
    """Refs of rows about one device."""
    return [k for k, r in refs.items() if r.fact.get("device_id") == device]


def test_disk_upgrade_passes_on_a_lasting_low_disk(fleet):
    """MT7PJB7N5LRE has been over the disk limit all month."""
    tenant = fleet.tenant("acme-001")
    refs = evidence(tenant, "low_storage")
    args = CreateUpgradeOrder(
        device_id="MT7PJB7N5LRE", component="disk", spec="2 TB SSD", refs=ref_for(refs, "MT7PJB7N5LRE")
    )
    p = propose(tenant, refs, args, 1, set())
    assert p.status == "awaiting_approval" and p.snapshots


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (
            CreateUpgradeOrder(device_id="Q3VUITEGQ400", component="memory", spec="32 GB", refs=["R1"]),
            "memory pressure",
        ),
        (FlagDeviceForReplacement(device_id="7VP16KHV88LM", reason="old", refs=["R1"]), "two lasting"),
        (
            OpenRemediationTicket(device_id="Q3VUITEGQ400", check_id="screen_lock", note="x", refs=["R1"]),
            "has not failed",
        ),
        (CreateUpgradeOrder(device_id="4SEFDLPTKXAM", component="disk", spec="1 TB", refs=["R1"]), "no device"),
        (NotifyEmployee(employee_id="emp-globex-1002", message="hi", refs=["R1"]), "no employee"),
    ],
)
def test_actions_without_the_evidence_are_refused(fleet, args, reason):
    """Healthy devices, single problems and other companies' devices or people never reach a person."""
    tenant = fleet.tenant("acme-001")
    refs = evidence(tenant, "battery_end_of_life")
    p = propose(tenant, refs, args, 1, set())
    assert p.status == "refused" and reason in p.reason


def test_replacement_passes_with_two_lasting_problems(fleet):
    """8NM23J95R5I6 has a worn battery and a full disk."""
    tenant = fleet.tenant("acme-001")
    refs = evidence(tenant, "battery_end_of_life")
    args = FlagDeviceForReplacement(
        device_id="8NM23J95R5I6", reason="battery and disk", refs=ref_for(refs, "8NM23J95R5I6")
    )
    assert propose(tenant, refs, args, 1, set()).status == "awaiting_approval"


def test_citations_must_exist_and_be_about_the_target(fleet):
    """No refs, unknown refs, and refs about another device are refused even when the rule holds."""
    tenant = fleet.tenant("acme-001")
    refs = evidence(tenant, "low_storage")
    base = {"device_id": "MT7PJB7N5LRE", "component": "disk", "spec": "2 TB"}
    assert "no evidence" in propose(tenant, refs, CreateUpgradeOrder(**base, refs=[]), 1, set()).reason
    assert "unknown refs" in propose(tenant, refs, CreateUpgradeOrder(**base, refs=["R99"]), 1, set()).reason
    other = ref_for(refs, "8NM23J95R5I6")
    assert "none of the cited rows" in propose(tenant, refs, CreateUpgradeOrder(**base, refs=other), 1, set()).reason


def test_the_same_action_twice_is_refused(fleet):
    """A key already proposed or executed cannot be proposed again."""
    tenant = fleet.tenant("acme-001")
    refs = evidence(tenant, "low_storage")
    args = CreateUpgradeOrder(
        device_id="MT7PJB7N5LRE", component="disk", spec="2 TB", refs=ref_for(refs, "MT7PJB7N5LRE")
    )
    first = propose(tenant, refs, args, 1, set())
    assert propose(tenant, refs, args, 2, {first.key}).status == "refused"


def test_notify_needs_a_lasting_problem_on_the_employees_device(fleet):
    """An employee whose devices are all fine gets no message proposal."""
    tenant = fleet.tenant("acme-001")
    rows = find_devices(tenant, FindDevices(employee_id="emp-acme-1003"))
    refs = {f"R{i + 1}": r for i, r in enumerate(rows)}
    p = propose(tenant, refs, NotifyEmployee(employee_id="emp-acme-1003", message="x", refs=list(refs)), 1, set())
    assert p.status == "awaiting_approval" and p.facts


def test_execution_happens_once_per_key(store):
    """Executing the same key twice returns the first reference."""
    first = store.execute("k1", "acme-001", "create_upgrade_order", "D1", {}, "ana", "")
    assert store.execute("k1", "acme-001", "create_upgrade_order", "D1", {}, "ana", "") == first
    assert len(store.actions("acme-001")) == 1 and store.actions("globex-002") == []


def test_the_audit_log_cannot_be_edited_or_deleted(store):
    """Triggers refuse updates and deletes."""
    store.log("acme-001", "s1", "ana", "question", {"text": "hi"})
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("update audit set event = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("delete from audit")
    assert store.audit("globex-002") == [] and len(store.audit("acme-001")) == 1


def test_a_proposal_row_is_not_evidence_for_another_proposal(fleet):
    """Citing an earlier proposal's row, which names the target, does not count as evidence."""
    from app.tools import Row

    tenant = fleet.tenant("acme-001")
    refs = {"R1": Row(ref="R1", tool="create_upgrade_order", fact={"target": "MT7PJB7N5LRE"}, snapshots=[])}
    args = CreateUpgradeOrder(device_id="MT7PJB7N5LRE", component="disk", spec="2 TB", refs=["R1"])
    assert "none of the cited rows" in propose(tenant, refs, args, 1, set()).reason
