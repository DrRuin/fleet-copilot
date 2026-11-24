"""Telemetry snapshots parsed once into typed records, and a per-company view that is the only way tools see data."""

import json
from collections import defaultdict
from datetime import datetime
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, computed_field

GB = 1024**3


class Check(BaseModel):
    """One compliance check result in a snapshot."""

    check_id: str
    status: str
    severity: str


class Battery(BaseModel):
    """Battery state when the agent reported one."""

    condition: str | None = None
    cycle_count: int | None = None
    full_charge_capacity: int | None = None
    percentage: int | None = None


class Snapshot(BaseModel):
    """One point-in-time report from a device, reduced to the fields the copilot reasons over."""

    id: str
    device_id: str
    company_id: str
    employee_id: str
    at: datetime
    hostname: str
    model_name: str
    processor: str
    os_name: str
    os_version: str
    ram_gb: float
    memory_used_pct: float | None
    disk_size_gb: float | None
    disk_used_pct: float | None
    disk_encrypted: bool | None
    battery: Battery | None
    software: list[str]
    checks: list[Check]
    network_reported: bool

    @classmethod
    def parse(cls, raw: dict) -> "Snapshot":
        """Build a snapshot from one raw telemetry line, keeping gaps as None rather than guessing."""
        memory = raw.get("memory") or {}
        total = memory.get("total_memory_bytes")
        volume = next(iter(raw.get("disk_volumes") or []), {})
        size = volume.get("size_bytes")
        battery = raw.get("battery") or {}
        return cls(
            id=f"{raw['device_id']}@{raw['collected_at']}",
            device_id=raw["device_id"],
            company_id=raw["company_id"],
            employee_id=raw["employee_id"],
            at=raw["collected_at"],
            hostname=raw["os"]["hostname"],
            model_name=raw["device_identity"]["model_name"],
            processor=raw["device_identity"]["processor"],
            os_name=raw["os"]["product_name"],
            os_version=raw["os"]["product_version"],
            ram_gb=(memory.get("ram_bytes") or 0) / GB,
            memory_used_pct=100 * memory["used_memory_bytes"] / total if total else None,
            disk_size_gb=round(size / GB, 1) if size else None,
            disk_used_pct=100 * (1 - volume["available_bytes"] / size) if size else None,
            disk_encrypted=volume.get("encrypted"),
            battery=Battery.model_validate(battery) if battery.get("battery_present") else None,
            software=[s["name"] for s in raw.get("installed_software") or []],
            checks=[Check.model_validate(c) for c in raw.get("compliance_results") or []],
            network_reported=bool(raw.get("network")),
        )

    @computed_field
    @property
    def os_major(self) -> int:
        """Leading number of the OS version, so macOS 14.5 is 14 and Windows 11 23H2 is 11."""
        return int(self.os_version.split(".")[0].split()[0])

    def failing(self) -> list[Check]:
        """Checks this snapshot reports as not passing."""
        return [c for c in self.checks if c.status != "pass"]


class Device(BaseModel):
    """A device and its snapshots in time order."""

    snapshots: list[Snapshot]

    @property
    def latest(self) -> Snapshot:
        """The most recent snapshot, the device's current state."""
        return self.snapshots[-1]

    @property
    def is_laptop(self) -> bool:
        """A device is a laptop when any of its snapshots reports a battery."""
        return any(s.battery for s in self.snapshots)


class Tenant:
    """One company's devices. Tools receive this and nothing wider, so no tool can reach another company."""

    def __init__(self, company_id: str, devices: dict[str, Device]):
        """Hold the company id and its devices."""
        self.company_id = company_id
        self.devices = devices

    def device(self, device_id: str) -> Device | None:
        """The device if it belongs to this company, else None with no hint that it exists elsewhere."""
        return self.devices.get(device_id)

    def snapshot(self, snapshot_id: str) -> Snapshot | None:
        """A snapshot by id if its device belongs to this company."""
        device = self.device(snapshot_id.split("@")[0])
        return next((s for s in device.snapshots if s.id == snapshot_id), None) if device else None

    @cached_property
    def employees(self) -> set[str]:
        """Employees with a device in this company."""
        return {d.latest.employee_id for d in self.devices.values()}


class Fleet:
    """Every company's telemetry, split into tenants at load."""

    def __init__(self, snapshots: list[Snapshot]):
        """Group snapshots by company and device, each device in time order."""
        grouped: dict[str, dict[str, list[Snapshot]]] = defaultdict(lambda: defaultdict(list))
        for s in snapshots:
            grouped[s.company_id][s.device_id].append(s)
        self.tenants = {
            company: Tenant(
                company,
                {d: Device(snapshots=sorted(v, key=lambda s: s.at)) for d, v in devices.items()},
            )
            for company, devices in grouped.items()
        }

    @classmethod
    def load(cls, path: Path) -> "Fleet":
        """Read the telemetry file."""
        with path.open(encoding="utf-8") as f:
            return cls([Snapshot.parse(json.loads(line)) for line in f if line.strip()])

    def tenant(self, company_id: str) -> Tenant:
        """The view for one company. An unknown company is refused, never widened."""
        if company_id not in self.tenants:
            raise KeyError(f"unknown company {company_id}")
        return self.tenants[company_id]
