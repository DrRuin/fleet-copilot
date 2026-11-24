"""Action tools the agent may propose, and the code rules that decide whether the evidence is enough to ask a person."""

import hashlib
import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.fleet import Device, Snapshot, Tenant
from app.tools import Row, streak

Refs = Field(description="Refs of the tool rows that show the problem, e.g. ['R3', 'R7']")


class CreateUpgradeOrder(BaseModel):
    """Propose ordering a hardware upgrade for one device. Needs a person's approval before anything is ordered."""

    device_id: str
    component: Literal["disk", "memory", "battery"]
    spec: str = Field(description="What to order, e.g. '1 TB SSD' or '32 GB RAM'")
    refs: list[str] = Refs


class OpenRemediationTicket(BaseModel):
    """Propose a ticket to fix a failing compliance check on one device. Needs a person's approval."""

    device_id: str
    check_id: str
    note: str
    refs: list[str] = Refs


class FlagDeviceForReplacement(BaseModel):
    """Propose flagging a device for replacement. Needs a person's approval."""

    device_id: str
    reason: str
    refs: list[str] = Refs


class NotifyEmployee(BaseModel):
    """Propose a message to the employee who uses a device. Needs a person's approval before it is sent."""

    employee_id: str
    message: str
    refs: list[str] = Refs


ActionArgs = CreateUpgradeOrder | OpenRemediationTicket | FlagDeviceForReplacement | NotifyEmployee
NAMES: dict[type[BaseModel], str] = {
    CreateUpgradeOrder: "create_upgrade_order",
    OpenRemediationTicket: "open_remediation_ticket",
    FlagDeviceForReplacement: "flag_device_for_replacement",
    NotifyEmployee: "notify_employee",
}
ACTION_KINDS = set(NAMES.values())


class Proposal(BaseModel):
    """An action the agent asked for, the facts code found, and where it stands."""

    id: str
    kind: str
    key: str
    target: str
    args: dict
    status: Literal["awaiting_approval", "refused", "approved", "rejected", "executed"]
    reason: str
    facts: list[str]
    snapshots: list[str]
    reference: str | None = None


def held(device: Device, test: Callable[[Snapshot], bool | None]) -> list[Snapshot]:
    """The latest run of snapshots where the condition holds, when it covers at least the required days."""
    run = streak(device.snapshots, test)
    return run if len(run) >= settings().evidence_days else []


def conditions(device: Device) -> dict[str, list[Snapshot]]:
    """Each hardware condition that has held on the device for the required days."""
    s = settings()
    found = {
        "low disk": held(device, lambda x: None if x.disk_used_pct is None else x.disk_used_pct >= s.low_disk_used),
        "memory pressure": held(
            device, lambda x: None if x.memory_used_pct is None else x.memory_used_pct >= s.high_memory_used
        ),
        "battery needs service": held(
            device,
            lambda x: (
                None
                if x.battery is None
                else x.battery.condition != "Normal" or (x.battery.cycle_count or 0) >= s.battery_cycle_limit
            ),
        ),
    }
    return {k: v for k, v in found.items() if v}


def failing(device: Device, check_id: str) -> list[Snapshot]:
    """Snapshots in the latest run where this check fails, when the run covers the required days."""
    return held(device, lambda x: next((c.status != "pass" for c in x.checks if c.check_id == check_id), None))


def describe(label: str, run: list[Snapshot]) -> str:
    """One line naming a condition and how long it has held."""
    return f"{label} in each of the last {len(run)} snapshots, since {run[0].at.date()}"


def judge(tenant: Tenant, args: ActionArgs) -> tuple[str, str | None, list[str], list[Snapshot]]:
    """The action's target, a refusal reason or None, the facts found and the snapshots behind them."""
    days = settings().evidence_days
    if isinstance(args, NotifyEmployee):
        devices = [d for d in tenant.devices.values() if d.latest.employee_id == args.employee_id]
        if not devices:
            return args.employee_id, f"no employee {args.employee_id} in this company", [], []
        facts, shots = [], []
        for d in devices:
            for label, run in conditions(d).items():
                facts.append(f"{d.latest.device_id}: {describe(label, run)}")
                shots += [run[0], run[-1]]
            for c in d.latest.failing():
                if run := failing(d, c.check_id):
                    facts.append(f"{d.latest.device_id}: {describe(c.check_id + ' failing', run)}")
                    shots += [run[0], run[-1]]
        reason = None if facts else f"none of the employee's devices has a problem lasting {days} days"
        return args.employee_id, reason, facts, shots
    device = tenant.device(args.device_id)
    if device is None:
        return args.device_id, f"no device {args.device_id} in this company", [], []
    found = conditions(device)
    facts = [describe(label, run) for label, run in found.items()]
    shots = [s for run in found.values() for s in (run[0], run[-1])]
    match args:
        case CreateUpgradeOrder(component=component):
            need = {"disk": "low disk", "memory": "memory pressure", "battery": "battery needs service"}[component]
            if need not in found:
                return device.latest.device_id, f"{need} has not held for {days} days", facts, shots
        case OpenRemediationTicket(check_id=check):
            run = failing(device, check)
            if not run:
                return device.latest.device_id, f"{check} has not failed for {days} days in a row", facts, shots
            facts.append(describe(f"{check} failing", run))
            shots += [run[0], run[-1]]
        case FlagDeviceForReplacement():
            if len(found) < 2:
                return (
                    device.latest.device_id,
                    "replacement needs two lasting hardware problems; an upgrade covers one",
                    facts,
                    shots,
                )
    return device.latest.device_id, None, facts, shots


def cited(refs: dict[str, Row], wanted: list[str], target: str) -> str | None:
    """A refusal when the agent cited no ref, an unknown ref, or no ref about the target."""
    if not wanted:
        return "no evidence was cited"
    missing = [r for r in wanted if r not in refs]
    if missing:
        return f"unknown refs {', '.join(missing)}"
    about = [
        r
        for r in wanted
        if refs[r].tool not in ACTION_KINDS
        and (
            target in (refs[r].fact.get("device_id"), refs[r].fact.get("employee_id"))
            or any(s.split("@")[0] == target for s in refs[r].snapshots)
        )
    ]
    return None if about else f"none of the cited rows is about {target}"


def propose(tenant: Tenant, refs: dict[str, Row], args: ActionArgs, number: int, done: set[str]) -> Proposal:
    """Check an action request against the evidence rules. Only a proposal that passes is put to a person."""
    kind = NAMES[type(args)]
    target, reason, facts, shots = judge(tenant, args)
    specific = {k: v for k, v in args.model_dump().items() if k in ("component", "check_id")}
    key = hashlib.sha256(json.dumps([tenant.company_id, kind, target, specific]).encode()).hexdigest()[:16]
    reason = reason or cited(refs, args.refs, target)
    if reason is None and key in done:
        reason = "the same action was already proposed or carried out"
    return Proposal(
        id=f"P{number}",
        kind=kind,
        key=key,
        target=target,
        args=args.model_dump(),
        status="refused" if reason else "awaiting_approval",
        reason=reason or "evidence meets the rule",
        facts=facts,
        snapshots=[s.id for s in shots],
    )
