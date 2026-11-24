"""Expected answers computed straight from the raw telemetry, independently of app.fleet and app.tools."""

import json
from collections import defaultdict
from pathlib import Path


def load(path: Path) -> dict[str, dict[str, list[dict]]]:
    """Raw snapshots grouped by company then device, in time order."""
    out: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out[r["company_id"]][r["device_id"]].append(r)
    return {c: {d: sorted(v, key=lambda r: r["collected_at"]) for d, v in ds.items()} for c, ds in out.items()}


def disk(r: dict) -> float | None:
    """Percent of the first volume used."""
    v = (r.get("disk_volumes") or [{}])[0]
    return 100 * (1 - v["available_bytes"] / v["size_bytes"]) if v.get("size_bytes") else None


def memory(r: dict) -> float | None:
    """Percent of RAM used."""
    m = r.get("memory") or {}
    return 100 * m["used_memory_bytes"] / m["total_memory_bytes"] if m.get("total_memory_bytes") else None


def battery(r: dict) -> dict | None:
    """The battery block when present."""
    b = r.get("battery") or {}
    return b if b.get("battery_present") else None


def laptop(snaps: list[dict]) -> bool:
    """Reports a battery at least once."""
    return any(battery(r) for r in snaps)


def failing(r: dict) -> list[dict]:
    """Checks not passing."""
    return [c for c in r.get("compliance_results") or [] if c["status"] != "pass"]


def mac_major(r: dict) -> int | None:
    """macOS major version, None for other systems."""
    return int(r["os"]["product_version"].split(".")[0]) if r["os"]["product_name"] == "macOS" else None


def share_over(snaps: list[dict], read, limit: float) -> float:
    """Share of reporting snapshots at or over the limit."""
    vals = [v for r in snaps if (v := read(r)) is not None]
    return sum(v >= limit for v in vals) / len(vals) if vals else 0.0


def cycles(snaps: list[dict]) -> int:
    """Charge cycles in the latest battery report, 0 when the device never reported one."""
    reports = [b for r in snaps if (b := battery(r))]
    return reports[-1]["cycle_count"] or 0 if reports else 0


def worn(snaps: list[dict], cycles: int) -> bool:
    """The latest battery report says it needs service or is past the cycle limit."""
    reports = [b for r in snaps if (b := battery(r))]
    return bool(reports) and (reports[-1]["condition"] != "Normal" or reports[-1]["cycle_count"] >= cycles)


def drifted(snaps: list[dict]) -> bool:
    """A check passed at some point, and fails from some day to the end."""
    for check in {c["check_id"] for r in snaps for c in r["compliance_results"]}:
        status = [next(c["status"] for c in r["compliance_results"] if c["check_id"] == check) for r in snaps]
        if status[-1] != "pass" and "pass" in status:
            last_pass = max(i for i, s in enumerate(status) if s == "pass")
            if all(s != "pass" for s in status[last_pass + 1 :]):
                return True
    return False


def new_apps(snaps: list[dict]) -> set[str]:
    """Apps missing on the first day and present on the last."""
    first = {s["name"] for s in snaps[0]["installed_software"]}
    return {s["name"] for s in snaps[-1]["installed_software"]} - first


def installed_later(snaps: list[dict]) -> bool:
    """An app missing on the first day is present on the last."""
    return bool(new_apps(snaps))
