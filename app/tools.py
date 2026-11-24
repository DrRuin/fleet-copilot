"""Read tools the agent calls. Code computes every figure, and every row carries a ref and the snapshot ids behind it."""

from collections import Counter
from collections.abc import Callable
from datetime import date
from math import ceil
from statistics import mean
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.fleet import Device, Snapshot, Tenant

Metric = Literal["disk", "memory", "battery", "compliance", "software", "os"]
Pattern = Literal[
    "battery_end_of_life",
    "memory_pressure",
    "low_storage",
    "compliance_drift",
    "new_software",
    "data_gaps",
]


class Row(BaseModel):
    """One fact a tool returns: what it says and the snapshots it rests on."""

    ref: str = ""
    tool: str
    fact: dict
    snapshots: list[str]


class FindDevices(BaseModel):
    """Which or how many devices match, on each device's latest snapshot; filters combine with AND."""

    os_name: Literal["macOS", "Windows"] | None = None
    os_major_below: int | None = Field(None, description="Only devices whose OS major version is lower, e.g. 15")
    model_contains: str | None = Field(None, description="Case-insensitive part of the model name, e.g. 'MacBook'")
    laptops_only: bool = Field(False, description="Only laptops (devices that report a battery)")
    disk_used_at_least: float | None = Field(None, description="Minimum percent of the system volume used")
    memory_used_at_least: float | None = Field(None, description="Minimum percent of RAM in use")
    failing_check: str | None = Field(None, description="A compliance check id that must be failing")
    failing_severity: Literal["low", "medium", "high"] | None = Field(
        None, description="Any failing check of this severity"
    )
    battery_condition: str | None = Field(None, description="Battery condition text, e.g. 'Service Recommended'")
    software: str | None = Field(None, description="Installed application name, case-insensitive")
    employee_id: str | None = None
    sort_by: Literal["disk_used", "memory_used", "battery_cycles", "device_id"] | None = Field(
        None, description="Row order. By default the metric filtered on, highest first, else device id"
    )
    page: int = Field(1, description="Page of rows, from 1. The count row gives the total and the number of pages")


class DeviceHistory(BaseModel):
    """The 30-day history of one metric for one device: daily values, first, last, minimum and maximum."""

    device_id: str
    metric: Metric


class DetectPatterns(BaseModel):
    """Trends over the whole window for every device: what was seen, since when, and the snapshots that show it."""

    pattern: Pattern
    page: int = Field(1, description="Page of findings, from 1. The count row gives the total and the number of pages")


class FleetOverview(BaseModel):
    """Counts across the company: devices, laptops, OS versions, models, compliance pass rates, employees, data gaps."""


def pct(x: float | None) -> float | None:
    """A percentage rounded for display; comparisons always use the exact reading."""
    return None if x is None else round(x, 1)


def latest_row(s: Snapshot, device: Device) -> dict:
    """The current-state fields shown for a device."""
    return {
        "device_id": s.device_id,
        "hostname": s.hostname,
        "employee_id": s.employee_id,
        "model": s.model_name,
        "laptop": device.is_laptop,
        "os": f"{s.os_name} {s.os_version}",
        "ram_gb": s.ram_gb,
        "disk_used_pct": pct(s.disk_used_pct),
        "disk_free_gb": round(s.disk_size_gb * (1 - s.disk_used_pct / 100), 1) if s.disk_size_gb else None,
        "memory_used_pct": pct(s.memory_used_pct),
        "battery": s.battery.model_dump() if s.battery else None,
        "failing_checks": [f"{c.check_id} ({c.severity})" for c in s.failing()],
        "as_of": s.at.isoformat(),
    }


def find_devices(tenant: Tenant, q: FindDevices) -> list[Row]:
    """Filter devices on their latest snapshot."""
    tests: list[Callable[[Snapshot, Device], bool]] = []
    if q.os_name:
        tests.append(lambda s, d: s.os_name == q.os_name)
    if q.os_major_below is not None:
        tests.append(lambda s, d: s.os_major < q.os_major_below)
    if q.model_contains:
        tests.append(lambda s, d: q.model_contains.lower() in s.model_name.lower())
    if q.laptops_only:
        tests.append(lambda s, d: d.is_laptop)
    if q.disk_used_at_least is not None:
        tests.append(lambda s, d: (s.disk_used_pct or 0) >= q.disk_used_at_least)
    if q.memory_used_at_least is not None:
        tests.append(lambda s, d: (s.memory_used_pct or 0) >= q.memory_used_at_least)
    if q.failing_check:
        tests.append(lambda s, d: any(c.check_id == q.failing_check for c in s.failing()))
    if q.failing_severity:
        tests.append(lambda s, d: any(c.severity == q.failing_severity for c in s.failing()))
    if q.battery_condition:
        tests.append(
            lambda s, d: bool(s.battery) and (s.battery.condition or "").lower() == q.battery_condition.lower()
        )
    if q.software:
        tests.append(lambda s, d: q.software.lower() in (n.lower() for n in s.software))
    if q.employee_id:
        tests.append(lambda s, d: s.employee_id == q.employee_id)
    pool = [d for d in tenant.devices.values() if not q.laptops_only or d.is_laptop]
    hits = [d for d in tenant.devices.values() if all(t(d.latest, d) for t in tests)]
    sort = q.sort_by or next(
        (
            k
            for k, on in (("disk_used", q.disk_used_at_least), ("memory_used", q.memory_used_at_least))
            if on is not None
        ),
        "battery_cycles" if q.battery_condition else "device_id",
    )
    label, key = SORTS[sort]
    hits.sort(key=lambda d: (-key(d.latest), d.latest.device_id) if key else (d.latest.device_id,))
    filters = q.model_dump(exclude_defaults=True, exclude={"sort_by", "page"})
    shown, page = paged(hits, q.page)
    rows = [
        Row(tool="find_devices", fact={**latest_row(d.latest, d), "matches_filters": filters}, snapshots=[d.latest.id])
        for d in shown
    ]
    count = {
        "matched": len(hits),
        "out_of": len(pool),
        "filters": filters,
        "basis": "latest snapshot per device",
        "ranked_by": label,
        **page,
        "by_model": tally(d.latest.model_name for d in hits),
        "by_os": tally(f"{d.latest.os_name} {d.latest.os_version}" for d in hits),
    }
    return [
        Row(tool="find_devices", fact=count, snapshots=[d.latest.id for d in hits]),
        *rows,
    ]


SORTS: dict[str, tuple[str, Callable[[Snapshot], float] | None]] = {
    "disk_used": ("disk used, highest first", lambda s: s.disk_used_pct or 0.0),
    "memory_used": ("memory used, highest first", lambda s: s.memory_used_pct or 0.0),
    "battery_cycles": (
        "battery charge cycles, highest first",
        lambda s: (s.battery.cycle_count or 0) if s.battery else 0,
    ),
    "device_id": ("device id", None),
}


def paged(items: list, page: int) -> tuple[list, dict]:
    """One page of an ordered list and where it sits in the whole."""
    size = settings().page_rows
    pages = max(1, ceil(len(items) / size))
    page = min(max(1, page), pages)
    first = (page - 1) * size
    shown = items[first : first + size]
    where = f"{first + 1}-{first + len(shown)} of {len(items)}" if shown else "none"
    return shown, {"page": page, "pages": pages, "showing": where, "next_page": page + 1 if page < pages else None}


def tally(values) -> dict[str, int]:
    """Counts of each value, largest first."""
    return dict(Counter(values).most_common())


def value(s: Snapshot, metric: Metric) -> object:
    """One snapshot's reading of a metric, None when the device did not report it."""
    match metric:
        case "disk":
            return pct(s.disk_used_pct)
        case "memory":
            return pct(s.memory_used_pct)
        case "battery":
            return s.battery.model_dump() if s.battery else None
        case "compliance":
            return {c.check_id: c.status for c in s.checks}
        case "software":
            return sorted(s.software)
        case "os":
            return f"{s.os_name} {s.os_version}"


def device_history(tenant: Tenant, q: DeviceHistory) -> list[Row]:
    """Daily readings of one metric for one device."""
    device = tenant.device(q.device_id)
    if device is None:
        return [
            Row(
                tool="device_history",
                fact={"error": f"no device {q.device_id} in {tenant.company_id}", "company_id": tenant.company_id},
                snapshots=[],
            )
        ]
    series = [{"date": s.at.date().isoformat(), "value": value(s, q.metric)} for s in device.snapshots]
    fact: dict = {"device_id": q.device_id, "metric": q.metric, "days": series}
    numbers = [p["value"] for p in series if isinstance(p["value"], float)]
    if numbers:
        fact |= {
            "first": numbers[0],
            "last": numbers[-1],
            "min": min(numbers),
            "max": max(numbers),
            "mean": round(mean(numbers), 1),
        }
    fact["missing_days"] = [p["date"] for p in series if p["value"] is None]
    return [Row(tool="device_history", fact=fact, snapshots=[s.id for s in device.snapshots])]


def streak(snapshots: list[Snapshot], test: Callable[[Snapshot], bool | None]) -> list[Snapshot]:
    """The run of latest snapshots that pass the test. A snapshot with no reading (None) neither breaks nor extends it."""
    run: list[Snapshot] = []
    for s in reversed(snapshots):
        verdict = test(s)
        if verdict is False:
            break
        if verdict:
            run.append(s)
    return run[::-1]


def share(snapshots: list[Snapshot], test: Callable[[Snapshot], bool | None]) -> tuple[float, list[Snapshot]]:
    """Share of reporting snapshots that pass the test, and those snapshots."""
    seen = [(s, test(s)) for s in snapshots]
    reported = [s for s, v in seen if v is not None]
    passed = [s for s, v in seen if v]
    return (len(passed) / len(reported) if reported else 0.0), passed


def finding(pattern: str, device: Device, text: str, stats: dict, evidence: list[Snapshot]) -> Row:
    """A detector result for one device."""
    latest = device.latest
    fact = {
        "pattern": pattern,
        "device_id": latest.device_id,
        "employee_id": latest.employee_id,
        "model": latest.model_name,
    }
    return Row(
        tool="detect_patterns",
        fact=fact | {"finding": text} | stats,
        snapshots=[s.id for s in evidence],
    )


def battery_end_of_life(tenant: Tenant) -> list[Row]:
    """Batteries the device reports as needing service, or past the cycle limit."""
    limit, rows = settings().battery_cycle_limit, []
    for d in tenant.devices.values():
        worn = streak(
            d.snapshots,
            lambda s: (
                None
                if s.battery is None
                else (s.battery.condition != "Normal" or (s.battery.cycle_count or 0) >= limit)
            ),
        )
        if not worn:
            continue
        service = streak(d.snapshots, lambda s: None if s.battery is None else s.battery.condition != "Normal")
        caps = [s.battery.full_charge_capacity for s in d.snapshots if s.battery and s.battery.full_charge_capacity]
        now = worn[-1].battery
        stats = {
            "condition": now.condition,
            "condition_since": service[0].at.date().isoformat() if service else None,
            "cycles": now.cycle_count,
            "cycle_limit": limit,
            "capacity_first": caps[0],
            "capacity_last": caps[-1],
            "capacity_drop_pct": round(100 * (1 - caps[-1] / caps[0]), 1),
        }
        parts = [
            f"reports '{now.condition}' since {stats['condition_since']}" if service else f"reports '{now.condition}'"
        ]
        parts.append(f"{now.cycle_count} cycles against a limit of {limit}")
        parts.append(f"full charge capacity down {stats['capacity_drop_pct']:g}% over the window")
        evidence = [service[0], worn[-1]] if service else [worn[0], worn[-1]]
        rows.append(finding("battery_end_of_life", d, "battery " + ", ".join(parts), stats, evidence))
    return rows


def sustained(
    tenant: Tenant,
    pattern: str,
    label: str,
    read: Callable[[Snapshot], float | None],
    limit: float,
) -> list[Row]:
    """Devices whose reading is at or over the limit in most snapshots, or over it now and rising."""
    rows, need = [], settings().consistent_share
    for d in tenant.devices.values():
        portion, over = share(d.snapshots, lambda s: None if read(s) is None else read(s) >= limit)
        readings = [r for s in d.snapshots if (r := read(s)) is not None]
        now = read(d.latest)
        if portion >= need:
            kind = "consistent"
        elif now is not None and now >= limit and readings[-1] > readings[0]:
            kind = "rising"
        else:
            continue
        stats = {
            "kind": kind,
            "share_over_limit": round(portion, 2),
            "limit_pct": limit,
            "first_pct": pct(readings[0]),
            "last_pct": pct(readings[-1]),
            "max_pct": pct(max(readings)),
        }
        text = f"{label} at or over {limit:g}% in {portion:.0%} of snapshots, now {pct(readings[-1]):g}% ({kind})"
        rows.append(finding(pattern, d, text, stats, [over[0], over[-1]] if over else [d.latest]))
    return rows


def compliance_drift(tenant: Tenant) -> list[Row]:
    """Checks that passed earlier in the window and fail in every snapshot since a given day."""
    rows = []
    for d in tenant.devices.values():
        for check in {c.check_id for c in d.latest.checks}:
            status = lambda s, check=check: next((c.status != "pass" for c in s.checks if c.check_id == check), None)
            failing = streak(d.snapshots, status)
            passed = [s for s in d.snapshots[: d.snapshots.index(failing[0])] if status(s) is False] if failing else []
            if not passed:
                continue
            last_pass = passed[-1]
            severity = next(c.severity for c in d.latest.checks if c.check_id == check)
            stats = {
                "check_id": check,
                "severity": severity,
                "failing_since": failing[0].at.date().isoformat(),
                "days_failing": len(failing),
            }
            text = f"{check} ({severity}) passed until {last_pass.at.date()} and has failed every day since {failing[0].at.date()}"
            rows.append(
                finding(
                    "compliance_drift",
                    d,
                    text,
                    stats,
                    [last_pass, failing[0], failing[-1]],
                )
            )
    return rows


def new_software(tenant: Tenant) -> list[Row]:
    """Applications absent from a device's first snapshot that appear later and stay."""
    rows = []
    for d in tenant.devices.values():
        for app in sorted(set(d.latest.software) - set(d.snapshots[0].software)):
            since = streak(d.snapshots, lambda s, app=app: app in s.software)
            text = f"{app} installed on {since[0].at.date()} and present since"
            rows.append(
                finding(
                    "new_software",
                    d,
                    text,
                    {
                        "application": app,
                        "installed_on": since[0].at.date().isoformat(),
                    },
                    [since[0], d.latest],
                )
            )
    return rows


def data_gaps(tenant: Tenant) -> list[Row]:
    """Laptops with snapshots that omit the battery, and devices with snapshots that omit network details."""
    rows = []
    for d in tenant.devices.values():
        no_battery = [s for s in d.snapshots if d.is_laptop and s.battery is None]
        no_network = [s for s in d.snapshots if not s.network_reported]
        if no_battery or no_network:
            stats = {
                "snapshots": len(d.snapshots),
                "missing_battery": len(no_battery),
                "missing_network": len(no_network),
            }
            text = f"{len(no_battery)} snapshots without battery data, {len(no_network)} without network data"
            rows.append(finding("data_gaps", d, text, stats, no_battery + no_network))
    return rows


def window(tenant: Tenant) -> dict:
    """First and last day of telemetry in the company, and the days between."""
    days = sorted({s.at.date() for d in tenant.devices.values() for s in d.snapshots})
    return {"from": days[0].isoformat(), "to": days[-1].isoformat(), "days": len(days)}


RULES: dict[str, Callable] = {
    "battery_end_of_life": lambda s: f"battery condition other than Normal, or {s.battery_cycle_limit} cycles or more",
    "memory_pressure": lambda s: (
        f"consistent: RAM {s.high_memory_used:g}% used or more in at least {s.consistent_share:.0%} of snapshots; "
        "rising: over that now and higher than at the start"
    ),
    "low_storage": lambda s: (
        f"consistent: disk {s.low_disk_used:g}% used or more in at least {s.consistent_share:.0%} of snapshots; "
        "rising: over that now and higher than at the start"
    ),
    "compliance_drift": lambda s: "a check that passed earlier and has failed in every snapshot since a given day",
    "new_software": lambda s: "an application absent from the first snapshot that is present in every one since",
    "data_gaps": lambda s: "laptop snapshots without battery data, and snapshots without network data",
}


def detect_patterns(tenant: Tenant, q: DetectPatterns) -> list[Row]:
    """Run one detector and add a count row."""
    s = settings()
    match q.pattern:
        case "battery_end_of_life":
            rows = battery_end_of_life(tenant)
        case "memory_pressure":
            rows = sustained(
                tenant,
                q.pattern,
                "memory use",
                lambda x: x.memory_used_pct,
                s.high_memory_used,
            )
        case "low_storage":
            rows = sustained(
                tenant,
                q.pattern,
                "disk use",
                lambda x: x.disk_used_pct,
                s.low_disk_used,
            )
        case "compliance_drift":
            rows = compliance_drift(tenant)
        case "new_software":
            rows = new_software(tenant)
        case "data_gaps":
            rows = data_gaps(tenant)
    label, key = RANKS[q.pattern]
    rows.sort(key=lambda r: key(r.fact))
    shown, page = paged(rows, q.page)
    found = {r.fact["device_id"]: r.fact["model"] for r in rows}
    count = {
        "pattern": q.pattern,
        "findings": len(rows),
        "devices_checked": len(tenant.devices),
        "devices_found": len(found),
        "window": window(tenant),
        "definition": RULES[q.pattern](s),
        "ranked_by": label,
        **page,
        "by_model": tally(found.values()),
    }
    for field, name in (("kind", "by_kind"), ("check_id", "by_check"), ("application", "by_application")):
        if values := [r.fact[field] for r in rows if field in r.fact]:
            count[name] = tally(values)
    return [
        Row(
            tool="detect_patterns",
            fact=count,
            snapshots=[i for r in rows for i in r.snapshots[-1:]],
        ),
        *shown,
    ]


SEVERITY = {"high": 0, "medium": 1, "low": 2}
RANKS: dict[str, tuple[str, Callable[[dict], tuple]]] = {
    "battery_end_of_life": ("charge cycles, highest first", lambda f: (-(f["cycles"] or 0), f["device_id"])),
    "memory_pressure": (
        "share of snapshots over the limit, then current reading, highest first",
        lambda f: (-f["share_over_limit"], -f["last_pct"], f["device_id"]),
    ),
    "low_storage": (
        "share of snapshots over the limit, then current reading, highest first",
        lambda f: (-f["share_over_limit"], -f["last_pct"], f["device_id"]),
    ),
    "compliance_drift": (
        "days failing, longest first, then severity",
        lambda f: (-f["days_failing"], SEVERITY.get(f["severity"], 3), f["device_id"], f["check_id"]),
    ),
    "new_software": (
        "install date, newest first",
        lambda f: (-date.fromisoformat(f["installed_on"]).toordinal(), f["application"], f["device_id"]),
    ),
    "data_gaps": (
        "snapshots missing data, most first",
        lambda f: (-(f["missing_battery"] + f["missing_network"]), f["device_id"]),
    ),
}


def fleet_overview(tenant: Tenant, _: FleetOverview) -> list[Row]:
    """Company-wide counts on the latest snapshots."""
    latest = [d.latest for d in tenant.devices.values()]
    tally = lambda key: {k: sum(1 for s in latest if key(s) == k) for k in sorted({key(s) for s in latest})}
    checks = sorted({c.check_id for s in latest for c in s.checks})
    passing = {
        c: sum(1 for s in latest if any(x.check_id == c and x.status == "pass" for x in s.checks)) for c in checks
    }
    fact = {
        "company_id": tenant.company_id,
        "devices": len(latest),
        "laptops": sum(d.is_laptop for d in tenant.devices.values()),
        "employees": len(tenant.employees),
        "os": tally(lambda s: f"{s.os_name} {s.os_version}"),
        "models": tally(lambda s: s.model_name),
        "checks_passing": passing,
        "window": f"{min(s.at for d in tenant.devices.values() for s in d.snapshots).date()} to {max(s.at for s in latest).date()}",
    }
    return [Row(tool="fleet_overview", fact=fact, snapshots=[s.id for s in latest])]


READS: dict[type[BaseModel], Callable[[Tenant, BaseModel], list[Row]]] = {
    FindDevices: find_devices,
    DeviceHistory: device_history,
    DetectPatterns: detect_patterns,
    FleetOverview: fleet_overview,
}
