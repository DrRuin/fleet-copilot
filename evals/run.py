"""Run the eval set against the live agent and score it: `uv run python -m evals.run [--only text] [--repeat n]`."""

import argparse
import asyncio
import json
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langgraph.types import Command

from app.config import ROOT, settings
from app.fleet import Fleet
from app.graph import Context, build, checkpoints, judge
from app.store import Store
from evals.cases import Case
from evals.cases import build as cases

RUNS = ROOT / "evals" / "runs"


def cost(usage: dict) -> float:
    """USD for the tokens each model used."""
    total = 0.0
    for model, u in usage.items():
        price = next((p for name, p in settings().prices.items() if model.startswith(name)), None)
        if price:
            cached = (u.get("input_token_details") or {}).get("cache_read", 0)
            total += ((u["input_tokens"] - cached) * price[0] + cached * price[1] + u["output_tokens"] * price[2]) / 1e6
    return total


def score(case: Case, answer, proposals: list, leaked: list[str], tenant) -> dict:
    """Pass or fail on each measure the case defines."""
    e, out = case.expect, {}
    named = {d for d in (answer.devices if answer else []) if d in tenant.devices} | {
        d for d, dev in tenant.devices.items() if answer and dev.latest.hostname in answer.devices
    }
    unknown = [
        d
        for d in (answer.devices if answer else [])
        if d not in named and not any(dev.latest.hostname == d for dev in tenant.devices.values())
    ]
    if e.partial:
        out["devices"] = bool(named) and named <= e.devices | e.allowed and not unknown and e.top in named | {None}
    elif e.devices is not None:
        out["devices"] = e.devices <= named <= e.devices | e.allowed and not unknown
    elif e.no_devices:
        out["devices"] = not named and not unknown
    elif e.allowed:
        out["devices"] = named <= e.allowed and not unknown
    if e.count is not None:
        out["count"] = answer is not None and answer.count in {e.count} | e.counts
    got = [(p.kind, p.target, p.status) for p in proposals if p.status != "refused"]
    want = [x for x in e.proposals if x[2] != "refused"]
    refusals = [(p.kind, p.target, p.status) for p in proposals if p.status == "refused"]
    owed = [x for x in e.proposals if x[2] == "refused" and x not in e.declinable]
    if e.proposals or got:
        out["actions"] = got == want and all(x in refusals for x in owed)
    out["isolation"] = not leaked
    return out


async def run_case(case: Case, fleet: Fleet, db: Path, foreign: dict[str, set[str]], audit_model: str) -> dict:
    """Play every turn of one case in a fresh session, answer each approval pause as the case says, and score."""
    tenant = fleet.tenant(case.company)
    store = Store(db)
    usage = UsageMetadataCallbackHandler()
    context = Context(tenant=tenant, store=store, session=uuid.uuid4().hex, actor="eval")
    config = {"configurable": {"thread_id": context.session}, "callbacks": [usage]}
    started, state, error = time.perf_counter(), {}, None
    try:
        async with checkpoints(db.with_suffix(".checkpoints.sqlite")) as saver:
            graph = build(saver)
            for turn in case.turns:
                state = await graph.ainvoke({"messages": [("user", turn)]}, config, context=context)
                while pauses := state.get("__interrupt__"):
                    decisions = {p["id"]: {"approve": case.approve} for p in pauses[0].value["proposals"]}
                    state = await graph.ainvoke(Command(resume=decisions), config, context=context)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    answer, checks, refs = state.get("answer"), state.get("checks", []), state.get("refs", {})
    shown = json.dumps([answer.model_dump() if answer else None, [r.model_dump() for r in refs.values()]], default=str)
    leaked = sorted(i for i in foreign[case.company] if i in shown and not any(i in t for t in case.turns))
    audited = []
    if answer and answer.sentences and not error:
        verdicts = await judge(audit_model, case.turns[-1], answer.sentences, refs)
        audited = [v.verdict for v in verdicts]
    result = {
        "id": case.id,
        "company": case.company,
        "category": case.category,
        "question": case.turns,
        "error": error,
        "answer": answer.model_dump() if answer else None,
        "proposals": [p.model_dump() for p in state.get("proposals", [])],
        "checks": checks,
        "withheld": sum(1 for c in checks if c["problem"]),
        "audit": audited,
        "leaked": leaked,
        "seconds": round(time.perf_counter() - started, 1),
        "usd": round(cost(usage.usage_metadata), 4),
        "calls": sum(1 for _ in usage.usage_metadata),
    }
    result["scores"] = {} if error else score(case, answer, state.get("proposals", []), leaked, tenant)
    result["passed"] = not error and all(result["scores"].values())
    return result


def summary(results: list[dict]) -> str:
    """A markdown table by category, then the grounding and cost lines."""
    by = defaultdict(list)
    for r in results:
        by[r["category"]].append(r)
    lines = ["| Category | Cases | Passed | Devices | Counts | Actions | Isolation |", "|---|---|---|---|---|---|---|"]

    def rate(rs: list[dict], key: str) -> str:
        """Passed over scored for one measure."""
        scored = [r["scores"][key] for r in rs if key in r["scores"]]
        return f"{sum(scored)}/{len(scored)}" if scored else ""

    for cat, rs in [*sorted(by.items()), ("all", results)]:
        passed = sum(r["passed"] for r in rs)
        cells = [rate(rs, k) for k in ("devices", "count", "actions", "isolation")]
        lines.append(f"| {cat} | {len(rs)} | {passed}/{len(rs)} | " + " | ".join(cells) + " |")
    sentences = [v for r in results for v in r["audit"]]
    claims = [v for v in sentences if v != "no_claim"]
    supported = sum(v == "supported" for v in claims)
    withheld = sum(r["withheld"] for r in results)
    leaks, errors = sum(bool(r["leaked"]) for r in results), sum(bool(r["error"]) for r in results)
    median = sorted(r["seconds"] for r in results)[len(results) // 2]
    lines += [
        "",
        f"Audited: {supported}/{len(claims)} factual sentences supported, {len(sentences) - len(claims)} with no claim.",
        f"Withheld by the runtime check: {withheld}. Leaks: {leaks}. Errors: {errors}.",
        f"Cost {sum(r['usd'] for r in results):.2f} USD, median {median} s a case.",
    ]
    return "\n".join(lines)


async def main(only: str | None, repeat: int) -> None:
    """Run the selected cases, write results and print the summary."""
    s = settings()
    fleet = Fleet.load(s.data)
    every = {
        c: {x for d in t.devices.values() for x in (d.latest.device_id, d.latest.hostname, d.latest.employee_id) if x}
        for c, t in fleet.tenants.items()
    }
    foreign = {c: set().union(*(v for k, v in every.items() if k != c)) for c in every}
    chosen = [c for c in cases() if not only or only in c.id]
    folder = RUNS / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    gate = asyncio.Semaphore(s.eval_concurrency)

    async def guarded(case: Case, n: int) -> dict:
        """One case under the concurrency limit, in its own database."""
        async with gate:
            result = await run_case(case, fleet, folder / f"{case.id.replace(':', '_')}-{n}.sqlite", foreign, s.model)
            mark = "PASS" if result["passed"] else "FAIL"
            print(f"{mark} {case.id} #{n} {result['scores']} {result['error'] or ''}", flush=True)
            return result | {"repeat": n}

    results = await asyncio.gather(*(guarded(c, n) for n in range(repeat) for c in chosen))
    (folder / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    table = summary(results)
    if repeat > 1:
        runs = defaultdict(list)
        for r in results:
            runs[r["id"]].append((r["passed"], tuple(sorted((r["answer"] or {}).get("devices", [])))))
        same = sum(len(set(v)) == 1 for v in runs.values())
        table += f"\nSame outcome and devices on all {repeat} repeats: {same}/{len(runs)} cases."
    (folder / "summary.md").write_text(table + "\n", encoding="utf-8")
    print("\n" + table + f"\n\nResults in {folder}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Run cases whose id contains this text")
    parser.add_argument("--repeat", type=int, default=1, help="Run each case this many times")
    args = parser.parse_args()
    asyncio.run(main(args.only, args.repeat))
