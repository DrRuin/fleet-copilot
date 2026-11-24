"""The tools agree with labels computed independently from the raw file, for every company."""

import pytest

from app.config import settings
from app.tools import DetectPatterns, DeviceHistory, FindDevices, detect_patterns, device_history, find_devices
from evals import labels as L

RAW = L.load(settings().data)
COMPANIES = sorted(RAW)


def found(rows) -> set[str]:
    """Device ids in the result rows after the count row."""
    return {r.fact["device_id"] for r in rows[1:]}


@pytest.mark.parametrize("company", COMPANIES)
def test_current_state_filters_match_the_raw_file(fleet, company):
    """Low disk, high-severity laptops and old macOS agree with the raw readings."""
    tenant, s, raw = fleet.tenant(company), settings(), RAW[company]
    low = {d for d, v in raw.items() if (L.disk(v[-1]) or 0) >= s.low_disk_used}
    assert found(find_devices(tenant, FindDevices(disk_used_at_least=s.low_disk_used))) == low
    high = find_devices(tenant, FindDevices(laptops_only=True, failing_severity="high"))
    assert high[0].fact["matched"] == 0
    old = {d for d, v in raw.items() if (m := L.mac_major(v[-1])) is not None and m < 15}
    assert found(find_devices(tenant, FindDevices(os_name="macOS", os_major_below=15))) == old


@pytest.mark.parametrize("company", COMPANIES)
def test_detectors_match_the_raw_file(fleet, company):
    """Each detector finds exactly the devices the independent labels find."""
    tenant, s, raw = fleet.tenant(company), settings(), RAW[company]

    def pattern(name: str) -> set[str]:
        """Devices a detector reports."""
        return found(detect_patterns(tenant, DetectPatterns(pattern=name)))

    assert pattern("battery_end_of_life") == {d for d, v in raw.items() if L.worn(v, s.battery_cycle_limit)}
    assert pattern("compliance_drift") == {d for d, v in raw.items() if L.drifted(v)}
    assert pattern("new_software") == {d for d, v in raw.items() if L.installed_later(v)}
    consistent = {d for d, v in raw.items() if L.share_over(v, L.disk, s.low_disk_used) >= s.consistent_share}
    rows = detect_patterns(tenant, DetectPatterns(pattern="low_storage"))
    assert {r.fact["device_id"] for r in rows[1:] if r.fact["kind"] == "consistent"} == consistent


def test_every_row_cites_snapshots_of_its_own_company(fleet):
    """Rows point at snapshots, and every snapshot belongs to the company asked about."""
    for company in COMPANIES:
        tenant = fleet.tenant(company)
        for name in ("battery_end_of_life", "memory_pressure", "low_storage", "compliance_drift", "new_software"):
            for row in detect_patterns(tenant, DetectPatterns(pattern=name))[1:]:
                assert row.snapshots
                assert all(tenant.snapshot(i) is not None for i in row.snapshots)


def test_a_device_of_another_company_does_not_exist_for_this_one(fleet):
    """History of a foreign device returns not found and cites nothing."""
    foreign = next(iter(fleet.tenant("globex-002").devices))
    rows = device_history(fleet.tenant("acme-001"), DeviceHistory(device_id=foreign, metric="disk"))
    assert "error" in rows[0].fact and rows[0].snapshots == []
    assert fleet.tenant("acme-001").snapshot(next(iter(fleet.tenant("globex-002").devices.values())).latest.id) is None


def test_thresholds_compare_exact_readings_not_rounded_ones(fleet):
    """A reading of 89.96% is under a 90% limit even though it shows as 90.0."""
    tenant = fleet.tenant("globex-002")
    near = [s for d in tenant.devices.values() for s in d.snapshots if 89.95 <= (s.disk_used_pct or 0) < 90]
    assert near, "the dataset has a reading just under the limit"
    assert all(s.disk_used_pct < settings().low_disk_used for s in near)


def test_a_long_list_comes_one_ranked_page_at_a_time(fleet, monkeypatch):
    """The count row covers every match, each page holds at most page_rows devices, and pages never overlap."""
    monkeypatch.setattr(settings(), "page_rows", 3)
    tenant = fleet.tenant("acme-001")
    first = find_devices(tenant, FindDevices(sort_by="disk_used"))
    count = first[0].fact
    assert (count["matched"], count["pages"], count["next_page"], count["showing"]) == (10, 4, 2, "1-3 of 10")
    assert sum(count["by_model"].values()) == 10 and len(first[0].snapshots) == 10
    seen = []
    for page in range(1, count["pages"] + 1):
        seen += [r.fact["device_id"] for r in find_devices(tenant, FindDevices(sort_by="disk_used", page=page))[1:]]
    disks = [tenant.device(d).latest.disk_used_pct for d in seen]
    assert len(seen) == len(set(seen)) == 10 and disks == sorted(disks, reverse=True)
    last = find_devices(tenant, FindDevices(sort_by="disk_used", page=99))
    assert last[0].fact["page"] == 4 and last[0].fact["next_page"] is None


def test_detector_pages_rank_findings_and_count_all_of_them(fleet, monkeypatch):
    """A detector's first page holds its top findings, while the count row and breakdowns cover every finding."""
    tenant = fleet.tenant("acme-001")
    every = detect_patterns(tenant, DetectPatterns(pattern="compliance_drift"))
    monkeypatch.setattr(settings(), "page_rows", 2)
    page = detect_patterns(tenant, DetectPatterns(pattern="compliance_drift"))
    assert page[0].fact["findings"] == every[0].fact["findings"] == len(every) - 1
    assert sum(page[0].fact["by_check"].values()) == len(every) - 1
    days = [r.fact["days_failing"] for r in every[1:]]
    assert days == sorted(days, reverse=True)
    assert [r.fact for r in page[1:]] == [r.fact for r in every[1:3]]
