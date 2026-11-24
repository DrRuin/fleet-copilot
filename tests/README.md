# tests

```bash
uv run pytest
```

33 tests on `data/telemetry.jsonl`, the telemetry provided with the brief. A scripted model stands in for OpenAI, so the suite makes no API calls and runs in about a second.

| File | Guards |
|---|---|
| `test_tools.py` | Every filter and detector returns exactly the devices the independent labels in `evals/labels.py` find, for every company. Pages never overlap and the count row covers every match. Thresholds compare exact readings, not rounded ones. Another company's device and snapshots return not found |
| `test_actions.py` | Each action passes only on its evidence rule. Single problems, healthy devices and other companies' devices or people are refused. Citations must exist and be about the target. The same action twice is refused, and executes once. The audit table refuses updates and deletes |
| `test_graph.py` | A supported answer cites rows from its tool call and is logged. An unsupported one goes back once, then is withheld. Unknown refs fail without the judge. An action waits for approval and runs once, a rejection runs nothing. The step budget forces an answer. A refused proposal does not block its corrected retry |

`conftest.py` loads the fleet once, gives each test an empty audit store, and replaces `graph.chat` with the scripted model.
