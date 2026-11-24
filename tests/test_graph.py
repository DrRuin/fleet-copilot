"""The agent loop on a scripted model: planning, tool rows, the evidence check, approval and the step budget."""

import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph import Context, Plan, Verdict, build


def plan_and(verdict: str):
    """Structured outputs: a fixed plan, and the given verdict for every sentence."""

    def make(schema, _messages):
        """Build the requested schema."""
        if schema is Plan:
            return Plan(intent="question", steps=["find_devices"], wants_action=False)
        return schema(**{name: Verdict(verdict=verdict, reason="scripted") for name in schema.model_fields})

    return make


def answer(text: str, refs: list[str], devices: list[str] | None = None) -> dict:
    """A final_answer call."""
    return {
        "name": "final_answer",
        "args": {"sentences": [{"text": text, "refs": refs}], "devices": devices or [], "count": None},
    }


async def run(graph, context, text: str, resume=None):
    """One turn, optionally resuming an approval."""
    config = {"configurable": {"thread_id": context.session}}
    if resume is not None:
        return await graph.ainvoke(Command(resume=resume), config, context=context)
    return await graph.ainvoke({"messages": [("user", text)]}, config, context=context)


def context(fleet, store, company="acme-001") -> Context:
    """A fresh session for one company."""
    return Context(tenant=fleet.tenant(company), store=store, session=uuid.uuid4().hex, actor="tester")


async def test_a_supported_answer_cites_rows_from_the_tool_call(fleet, store, script):
    """The rows get refs, the answer cites them, and the audit log records each step."""
    script(
        [
            [{"name": "find_devices", "args": {"disk_used_at_least": 90}}],
            [answer("Two devices are low on disk.", ["R1"], ["MT7PJB7N5LRE"])],
        ],
        plan_and("supported"),
    )
    ctx = context(fleet, store)
    state = await run(build(InMemorySaver()), ctx, "Which devices are low on disk?")
    assert state["refs"]["R1"].fact["matched"] == 2
    assert state["answer"].sentences[0].refs == ["R1"]
    assert [e["event"] for e in reversed(store.audit("acme-001"))] == ["question", "tool", "answer"]


async def test_an_unsupported_answer_is_sent_back_then_withheld(fleet, store, script):
    """A sentence the judge rejects goes back once, and is removed if it is still unsupported."""
    script(
        [
            [{"name": "fleet_overview", "args": {}}],
            [answer("All disks are fine.", ["R1"])],
            [answer("All disks are fine.", ["R1"])],
        ],
        plan_and("unsupported"),
    )
    ctx = context(fleet, store)
    state = await run(build(InMemorySaver()), ctx, "Are disks fine?")
    assert state["rewrites"] == 1
    assert state["answer"].sentences[0].text.startswith("I could not support")
    assert any(e["event"] == "answer_rejected" for e in store.audit("acme-001"))


async def test_unknown_refs_are_rejected_without_the_judge(fleet, store, script):
    """Citing a ref no tool returned fails the code check."""
    script([[answer("Disk is low.", ["R9"])], [answer("Disk is low.", ["R9"])]], plan_and("supported"))
    state = await run(build(InMemorySaver()), context(fleet, store), "Disk?")
    assert "unknown refs" in state["checks"][0]["problem"]


async def test_an_action_waits_for_approval_and_runs_once(fleet, store, script):
    """A proposal that meets the rule pauses the graph; approval executes it exactly once."""
    order = {"device_id": "MT7PJB7N5LRE", "component": "disk", "spec": "2 TB SSD", "refs": ["R2"]}
    script(
        [
            [{"name": "detect_patterns", "args": {"pattern": "low_storage"}}],
            [{"name": "create_upgrade_order", "args": order}],
            [answer("I proposed a disk upgrade.", ["R4"])],
        ],
        plan_and("supported"),
    )
    graph, ctx = build(InMemorySaver()), context(fleet, store)
    state = await run(graph, ctx, "Order a disk upgrade for MT7PJB7N5LRE")
    pending = state["__interrupt__"][0].value["proposals"]
    assert [p["status"] for p in pending] == ["awaiting_approval"]
    assert store.actions("acme-001") == []
    state = await run(graph, ctx, "", resume={pending[0]["id"]: {"approve": True}})
    assert [a["target"] for a in store.actions("acme-001")] == ["MT7PJB7N5LRE"]
    assert state["proposals"][0].status == "executed"


async def test_a_rejected_action_is_not_executed(fleet, store, script):
    """Rejection records the decision and executes nothing."""
    ticket = {"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "fix", "refs": ["R2"]}
    script(
        [
            [{"name": "find_devices", "args": {"failing_check": "screen_lock"}}],
            [{"name": "open_remediation_ticket", "args": ticket}],
            [answer("I proposed a ticket.", ["R7"])],
        ],
        plan_and("supported"),
    )
    graph, ctx = build(InMemorySaver()), context(fleet, store)
    state = await run(graph, ctx, "Open a ticket")
    pending = state["__interrupt__"][0].value["proposals"]
    state = await run(graph, ctx, "", resume={pending[0]["id"]: {"approve": False, "note": "not now"}})
    assert store.actions("acme-001") == []
    assert state["proposals"][0].status == "rejected"


async def test_the_step_budget_forces_an_answer(fleet, store, script, monkeypatch):
    """Past the budget the agent is offered only final_answer, so the loop ends."""
    from app import graph as g

    monkeypatch.setattr(g.settings(), "max_steps", 2)
    script(
        [
            [{"name": "fleet_overview", "args": {}}],
            [{"name": "fleet_overview", "args": {}}],
            [answer("Ten devices.", ["R1"])],
        ],
        plan_and("supported"),
    )
    state = await run(build(InMemorySaver()), context(fleet, store), "Overview")
    assert state["steps"] == 3 and state["answer"].sentences[0].text == "Ten devices."


async def test_a_refused_proposal_does_not_block_its_corrected_retry(fleet, store, script):
    """Bad refs are refused; the same action with the right refs in the same turn still reaches a person."""
    order = {"device_id": "MT7PJB7N5LRE", "component": "disk", "spec": "2 TB SSD"}
    script(
        [
            [{"name": "detect_patterns", "args": {"pattern": "low_storage"}}],
            [{"name": "create_upgrade_order", "args": {**order, "refs": ["R99"]}}],
            [{"name": "create_upgrade_order", "args": {**order, "refs": ["R2"]}}],
            [answer("I proposed a disk upgrade.", ["R5"])],
        ],
        plan_and("supported"),
    )
    state = await run(build(InMemorySaver()), context(fleet, store), "Order a disk upgrade")
    assert [p["status"] for p in state["__interrupt__"][0].value["proposals"]] == ["awaiting_approval"]


async def test_a_withheld_answer_names_no_devices(fleet, store, script):
    """When every sentence fails the check, the device list and count go with them."""
    script(
        [
            [{"name": "fleet_overview", "args": {}}],
            [answer("X.", ["R1"], ["MT7PJB7N5LRE"])],
            [answer("X.", ["R1"], ["MT7PJB7N5LRE"])],
        ],
        plan_and("unsupported"),
    )
    state = await run(build(InMemorySaver()), context(fleet, store), "?")
    assert state["answer"].devices == [] and state["answer"].count is None


def test_unanswered_tool_calls_get_a_placeholder_result():
    """A tool call a failed run left without a result is answered before the next model call."""
    from langchain.messages import AIMessage, HumanMessage, ToolMessage

    from app.graph import answered

    history = [HumanMessage("hi"), AIMessage("", tool_calls=[{"name": "fleet_overview", "args": {}, "id": "c1"}])]
    fixed = answered([*history, HumanMessage("again")])
    assert isinstance(fixed[2], ToolMessage) and fixed[2].tool_call_id == "c1"
