# data

| File | Companies | Devices | Snapshots | In git |
|---|---|---|---|---|
| `telemetry.jsonl` | Acme (10), Globex (8), Initech (7) | 25 | 750 | Yes, the telemetry provided with the brief |
| `large/telemetry.jsonl` | The three above, Harbor (5,000), Meridian (3,000), Summit (2,000) | 10,025 | 300,750 | No, 555 MB, generated |

```bash
uv run python -m app.synth        # writes data/large/telemetry.jsonl in about 12 s
DATA=data/large/telemetry.jsonl uv run uvicorn app.main:app --port 8000
```

`app/synth.py` copies the brief's 25 devices, then writes the three generated companies in the same format, 30 daily snapshots each from 2026-05-14. The seed is fixed, so every run writes the same file.

## What the generator plants

| Pattern | Share of devices |
|---|---|
| Disk filling over the month | 7% |
| Disk full all month | 3% |
| Memory pressure | 5% |
| Screen lock off | 22% |
| A check that passes, then fails to the end | 6% |
| Unencrypted disk | 1.5% |
| Days with no network or battery reading | 6% and 5% |
| uTorrent, TeamViewer or CleanMyMac X installed mid-month | 4%, 1.5%, 3% |
| An employee with a second device | 12% |

Models, OS versions, checks and field shapes follow the brief's devices. Batteries age with cycles and turn to Service Recommended near 1,000. Only the current OS versions (macOS 15.4, Windows 11 23H2) pass the OS check.
