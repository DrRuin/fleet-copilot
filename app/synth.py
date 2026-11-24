"""Write the brief's 25 devices plus a large generated fleet in the same telemetry format: `uv run python -m app.synth`."""

import argparse
import json
import random
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import ROOT

GB = 1024**3
BRIEF = ROOT / "data" / "telemetry.jsonl"
START = datetime(2026, 5, 14, tzinfo=UTC)
DAYS = 30
COMPANIES = {"harbor-101": 5000, "meridian-102": 3000, "summit-103": 2000}
BASE_APPS = [
    ("Google Chrome", "126.0", "Google LLC"),
    ("Slack", "4.39", "Slack Technologies"),
    ("Zoom", "6.1.1", "Zoom Video"),
    ("1Password", "8.10", "AgileBits"),
    ("Visual Studio Code", "1.90", "Microsoft"),
]
LATER_APPS = {
    "uTorrent": ("3.6", "BitTorrent Inc", 0.04, None),
    "TeamViewer": ("15.54", "TeamViewer", 0.015, None),
    "CleanMyMac X": ("4.15", "MacPaw", 0.03, "macOS"),
}
CURRENT = {"macOS": "15.4", "Windows": "11 23H2"}
RATES = {
    "filling_disk": 0.07,
    "full_disk": 0.03,
    "memory_pressure": 0.05,
    "unencrypted": 0.015,
    "screen_lock_off": 0.22,
    "screen_lock_drift": 0.06,
    "network_gaps": 0.06,
    "battery_gaps": 0.05,
    "second_device": 0.12,
}


@dataclass(frozen=True)
class Model:
    """A hardware model as the real fleet reports it."""

    name: str
    identifier: str
    processor: str
    os: str
    versions: tuple[tuple[str, str, str], ...]
    ram: tuple[int, ...]
    disk: tuple[int, ...]
    laptop: bool
    capacity: int
    weight: float


MAC = (
    ("13.6", "22G120", "22.6.0"),
    ("14.5", "23F79", "23.5.0"),
    ("15.3", "24D60", "24.3.0"),
    ("15.4", "24E248", "24.4.0"),
)
WIN = (("10 22H2", "19045", "10.0.19045"), ("11 22H2", "22621", "10.0.22621"), ("11 23H2", "22631", "10.0.22631"))
MODELS = [
    Model("MacBook Pro", "Mac15,7", "Apple M3 Pro", "macOS", MAC, (18, 36), (512, 1024), True, 6100, 0.2),
    Model("MacBook Pro", "Mac14,9", "Apple M2 Pro", "macOS", MAC, (16, 32), (512, 1024), True, 5800, 0.2),
    Model("MacBook Air", "Mac14,2", "Apple M2", "macOS", MAC, (8, 16), (256, 512), True, 4100, 0.16),
    Model("Mac mini", "Mac14,3", "Apple M2", "macOS", MAC, (8, 16, 24), (256, 512), False, 0, 0.12),
    Model("ThinkPad X1 Carbon", "21CB", "Intel Core i7-1365U", "Windows", WIN, (16, 32), (512, 1024), True, 5000, 0.16),
    Model("Dell XPS 15", "9530", "Intel Core i7-13700H", "Windows", WIN, (16, 32), (512, 1024), True, 4600, 0.16),
]


def serial(rng: random.Random) -> str:
    """A 12-character serial number like the real ones."""
    return "".join(rng.choices(string.ascii_uppercase + string.digits, k=12))


def path(rng: random.Random, start: float, end: float, wobble: float) -> list[float]:
    """A daily series that moves from start toward end with noise, kept between 1 and 99.5 percent."""
    return [
        min(99.5, max(1.0, start + (end - start) * day / (DAYS - 1) + rng.uniform(-wobble, wobble)))
        for day in range(DAYS)
    ]


def device(rng: random.Random, company: str, number: int, employee: str) -> list[dict]:
    """Thirty daily snapshots of one generated device."""
    model = rng.choices(MODELS, weights=[m.weight for m in MODELS])[0]
    version, build, kernel = rng.choice(model.versions)
    ram, size = rng.choice(model.ram) * GB, rng.choice(model.disk) * GB
    device_id = serial(rng)
    if rng.random() < RATES["full_disk"]:
        disk = path(rng, rng.uniform(91, 95), rng.uniform(96, 99), 0.6)
    elif rng.random() < RATES["filling_disk"]:
        disk = path(rng, rng.uniform(78, 88), rng.uniform(95, 98.5), 0.8)
    else:
        level = rng.uniform(30, 80)
        disk = path(rng, level, level + rng.uniform(-4, 5), 1.5)
    if rng.random() < RATES["memory_pressure"]:
        memory = path(rng, rng.uniform(86, 93), rng.uniform(88, 99), 1.5)
    else:
        level = rng.uniform(28, 72)
        memory = path(rng, level, level + rng.uniform(-6, 6), 3.0)
    cycles = rng.randint(15, 1180)
    service_at = max(0, int((1000 - cycles) / 1.1)) if cycles > 900 else DAYS
    capacity = int(model.capacity * rng.uniform(0.88, 1.02))
    encrypted = rng.random() >= RATES["unencrypted"]
    lock_off = rng.random() < RATES["screen_lock_off"]
    lock_from = rng.randint(4, DAYS - 4) if not lock_off and rng.random() < RATES["screen_lock_drift"] else DAYS
    installs = {
        app: rng.randint(3, DAYS - 3)
        for app, (_, _, rate, only) in LATER_APPS.items()
        if (only is None or only == model.os) and rng.random() < rate
    }
    no_network = set(rng.sample(range(DAYS), rng.randint(1, 6))) if rng.random() < RATES["network_gaps"] else set()
    no_battery = (
        set(rng.sample(range(DAYS), rng.randint(1, 5)))
        if model.laptop and rng.random() < RATES["battery_gaps"]
        else set()
    )
    host = f"{company.split('-')[0]}-{model.name.lower().replace(' ', '')}-{number}"
    windows = model.os == "Windows"
    page = 4096 if windows else 16384
    snaps = []
    for day in range(DAYS):
        at = START + timedelta(days=day, hours=8, minutes=rng.randint(0, 180))
        used_memory = int(ram * memory[day] / 100)
        today = cycles + day * rng.choice((0, 1, 1, 2))
        worn = day >= service_at
        battery = None
        if model.laptop and day not in no_battery:
            battery = {
                "battery_present": True,
                "charging_status": rng.choice(("charging", "charged", "discharging")),
                "percentage": rng.randint(5, 100),
                "condition": "Service Recommended" if worn else "Normal",
                "cycle_count": today,
                "full_charge_capacity": int(capacity * (0.96 if worn else 1.0)) - (day if worn else 0),
            }
        checks = [
            {"check_id": "disk_encryption", "status": "pass" if encrypted else "fail", "severity": "high"},
            {
                "check_id": "screen_lock",
                "status": "fail" if lock_off or day >= lock_from else "pass",
                "severity": "medium",
            },
            {
                "check_id": "os_up_to_date",
                "status": "pass" if version == CURRENT[model.os] else "fail",
                "severity": "medium",
            },
        ]
        software = [{"name": n, "version": v, "publisher": p} for n, v, p in BASE_APPS]
        software += [
            {"name": app, "version": LATER_APPS[app][0], "publisher": LATER_APPS[app][1]}
            for app, since in installs.items()
            if day >= since
        ]
        snaps.append(
            {
                "device_id": device_id,
                "company_id": company,
                "employee_id": employee,
                "collected_at": at.strftime("%Y-%m-%dT%H:%M:00Z"),
                "agent_version": "42.0.0",
                "os": {
                    "platform": "win32" if windows else "darwin",
                    "product_name": model.os,
                    "product_version": version,
                    "build_version": build,
                    "architecture": "x64" if windows else "arm64",
                    "kernel_name": "Windows_NT" if windows else "Darwin",
                    "kernel_release": kernel,
                    "hostname": host,
                },
                "device_identity": {
                    "serial_number": device_id,
                    "model_name": model.name,
                    "model_identifier": model.identifier,
                    "processor": model.processor,
                    "hardware_uuid": str(uuid.UUID(int=rng.getrandbits(128))),
                    "total_memory": f"{ram // GB} GB",
                },
                "memory": {
                    "ram_bytes": ram,
                    "total_memory_bytes": ram,
                    "used_memory_bytes": used_memory,
                    "free_memory_bytes": ram - used_memory,
                    "page_size_bytes": page,
                },
                "disk_volumes": [
                    {
                        "volume_name": "OS" if windows else "Macintosh HD",
                        "file_system": "ntfs" if windows else "apfs",
                        "mount_point": "C:\\" if windows else "/",
                        "size_bytes": size,
                        "available_bytes": int(size * (1 - disk[day] / 100)),
                        "encrypted": encrypted,
                    }
                ],
                "battery": battery,
                "network": []
                if day in no_network
                else [
                    {
                        "address": f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
                        "family": "IPv4",
                        "interface_name": "Ethernet" if windows else "en0",
                        "internal": False,
                        "mac": ":".join(f"{rng.randint(0, 255):02x}" for _ in range(6)),
                    }
                ],
                "installed_software": software,
                "compliance_results": checks,
            }
        )
    return snaps


def generate(out: Path, seed: int) -> int:
    """Copy the brief's snapshots, then write every generated company's, and return how many devices were made."""
    rng, made = random.Random(seed), 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.writelines(line.rstrip("\r\n") + "\n" for line in BRIEF.open(encoding="utf-8") if line.strip())
        for company, size in COMPANIES.items():
            prefix, employee, number = company.split("-")[0], 10000, 0
            while number < size:
                owner = f"emp-{prefix}-{employee}"
                for _ in range(2 if rng.random() < RATES["second_device"] and number + 1 < size else 1):
                    number += 1
                    for snap in device(rng, company, number, owner):
                        f.write(json.dumps(snap) + "\n")
                employee += 1
            made += size
    return made


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "large" / "telemetry.jsonl")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    print(f"{generate(args.out, args.seed)} devices written to {args.out}")
