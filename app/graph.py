"""The agent: plan, call tools in an explicit loop, check the answer against its evidence, then ask for approval."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from operator import or_
from pathlib import Path
from typing import Annotated, Literal

import aiosqlite
from langchain.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, create_model
from typing_extensions import TypedDict

from app import prompts
from app.actions import NAMES, Proposal, propose
from app.config import settings
from app.fleet import Tenant
from app.store import Store
from app.tools import READS, DetectPatterns, DeviceHistory, FindDevices, FleetOverview, Row

TOOL_NAMES = {FindDevices: "find_devices", DeviceHistory: "device_history", DetectPatterns: "detect_patterns"}
TOOL_NAMES |= {FleetOverview: "fleet_overview"} | NAMES


class Plan(BaseModel):
    """How the copilot will handle the message."""

    intent: Literal["question", "insight", "action", "other_company", "out_of_scope"]
    steps: list[str] = Field(description="Tool calls in order, each with what it should establish")
    wants_action: bool = Field(description="True when the administrator asks for something to be done")


class Sentence(BaseModel):
    """One sentence of the answer and the refs of the rows that support it."""

    text: str
    refs: list[str] = Field(description="Refs such as R3 for every fact in the sentence; empty only for no fact")


class FinalAnswer(BaseModel):
    """Finish the turn with the answer to the administrator, one cited sentence at a time."""

    sentences: list[Sentence]
    devices: list[str] = Field(description="Ids of the devices the answer names as matching or affected, else empty")
    count: int | None = Field(
        description="The total the answer gives for a 'how many' question or for a list shown in part, else null"
    )


class Verdict(BaseModel):
    """The judge's reading of one sentence."""

    verdict: Literal["supported", "unsupported", "no_claim"]
    reason: str


def merge(old: list[Proposal], new: list[Proposal]) -> list[Proposal]:
    """Proposals keyed by id, later versions replacing earlier ones."""
    return list(({p.id: p for p in old} | {p.id: p for p in new}).values())


class State(TypedDict, total=False):
    """What the graph carries through a conversation and checkpoints after every step."""

    messages: Annotated[list[AnyMessage], add_messages]
    refs: Annotated[dict[str, Row], or_]
    proposals: Annotated[list[Proposal], merge]
    plan: Plan
    answer: FinalAnswer
    checks: list[dict]
    steps: int
    rewrites: int
    turn: int


@dataclass
class Context:
    """Set by the server from the signed-in session, never by the model."""

    tenant: Tenant
    store: Store
    session: str
    actor: str


def spec(cls: type[BaseModel], name: str) -> dict:
    """An OpenAI tool definition for a schema, under the name the model sees."""
    tool = convert_to_openai_tool(cls, strict=True)
    tool["function"]["name"] = name
    return tool


def chat(model: str) -> ChatOpenAI:
    """A Responses API chat model."""
    key = settings().openai_api_key
    return ChatOpenAI(model=model, use_responses_api=True, api_key=key.get_secret_value() if key else None)


def audit(runtime: Runtime[Context], event: str, detail: dict) -> None:
    """Write one audit event for this session."""
    c = runtime.context
    c.store.log(c.tenant.company_id, c.session, c.actor, event, detail)


def question(state: State) -> str:
    """The administrator's latest message."""
    return next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))


async def plan(state: State, runtime: Runtime[Context]) -> dict:
    """Read the message and decide the tool steps before any tool runs."""
    company = runtime.context.tenant.company_id
    planner = chat(settings().model).with_structured_output(Plan, strict=True)
    result = await planner.ainvoke([SystemMessage(prompts.PLANNER.format(company=company)), *state["messages"]])
    audit(runtime, "question", {"text": question(state), "plan": result.model_dump()})
    return {"plan": result, "steps": 0, "rewrites": 0, "turn": state.get("turn", 0) + 1, "checks": []}


async def agent(state: State, runtime: Runtime[Context]) -> dict:
    """One model call that must call a tool; past the step budget only final_answer is offered."""
    s, company = settings(), runtime.context.tenant.company_id
    tools = [spec(FinalAnswer, "final_answer")]
    if state["steps"] < s.max_steps:
        tools += [spec(cls, name) for cls, name in TOOL_NAMES.items()]
    model = chat(s.model).bind_tools(tools, tool_choice="required", strict=True)
    system = prompts.AGENT.format(
        company=company, plan=state["plan"].model_dump_json(), definitions=prompts.definitions()
    )
    reply = await model.ainvoke([SystemMessage(system), *answered(state["messages"])])
    return {"messages": [reply], "steps": state["steps"] + 1}


def answered(messages: list) -> list:
    """The history with a placeholder result after any tool call a failed run left unanswered."""
    results = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    out = []
    for m in messages:
        out.append(m)
        if isinstance(m, AIMessage):
            out += [ToolMessage("not run", tool_call_id=c["id"]) for c in m.tool_calls if c["id"] not in results]
    return out


def route(state: State) -> Literal["act", "verify"]:
    """To verification when the model gave its answer, else run its tool calls."""
    calls = state["messages"][-1].tool_calls
    return "verify" if any(c["name"] == "final_answer" for c in calls) else "act"


async def act(state: State, runtime: Runtime[Context]) -> dict:
    """Run each tool call: reads return numbered rows, action calls become proposals checked by code."""
    tenant, refs = runtime.context.tenant, dict(state.get("refs", {}))
    proposals = list(state.get("proposals", []))
    done = {p.key for p in proposals if p.status != "refused"} | {
        a["key"] for a in runtime.context.store.actions(tenant.company_id)
    }
    by_name = {name: cls for cls, name in TOOL_NAMES.items()}
    messages, new_refs, new_proposals = [], {}, []
    for call in state["messages"][-1].tool_calls:
        cls = by_name[call["name"]]
        args = cls.model_validate(call["args"])
        if cls in READS:
            rows = READS[cls](tenant, args)
            for row in rows:
                row.ref = f"R{len(refs) + 1}"
                refs[row.ref] = new_refs[row.ref] = row
            content = json.dumps([{"ref": r.ref, **r.fact} for r in rows], default=str)
            audit(runtime, "tool", {"tool": call["name"], "args": call["args"], "refs": [r.ref for r in rows]})
        else:
            proposal = propose(tenant, refs, args, len(proposals) + 1, done)
            proposals.append(proposal)
            new_proposals.append(proposal)
            if proposal.status != "refused":
                done.add(proposal.key)
            shown = proposal.model_dump(include={"id", "kind", "target", "status", "reason", "facts"})
            row = Row(ref=f"R{len(refs) + 1}", tool=proposal.kind, fact=shown, snapshots=proposal.snapshots)
            refs[row.ref] = new_refs[row.ref] = row
            content = json.dumps({"ref": row.ref, **shown})
            audit(runtime, "proposal", proposal.model_dump())
        messages.append(ToolMessage(content, tool_call_id=call["id"]))
    return {"messages": messages, "refs": new_refs, "proposals": new_proposals}


def evidence(refs: dict[str, Row], cited: list[str]) -> str:
    """The cited rows as the judge reads them."""
    return json.dumps({r: refs[r].fact for r in cited if r in refs}, default=str)


async def judge(model: str, asked: str, sentences: list[Sentence], refs: dict[str, Row]) -> list[Verdict]:
    """One structured call that reads every sentence against only the rows it cites, a field per sentence."""
    fields = {f"s{i}": (Verdict, ...) for i in range(len(sentences))}
    schema = create_model("Verdicts", __doc__="One verdict per sentence, in order.", **fields)
    shown = "\n".join(f"s{i}: {s.text}\ncited rows: {evidence(refs, s.refs)}" for i, s in enumerate(sentences))
    judged = await (
        chat(model)
        .with_structured_output(schema, strict=True)
        .ainvoke([SystemMessage(prompts.JUDGE), HumanMessage(f"Question: {asked}\n\n{shown}")])
    )
    return [getattr(judged, f"s{i}") for i in range(len(sentences))]


async def verify(state: State, runtime: Runtime[Context]) -> Command:
    """Check the answer: code rejects unknown refs, a judge model reads each sentence against its cited rows."""
    call = next(c for c in state["messages"][-1].tool_calls if c["name"] == "final_answer")
    answer = FinalAnswer.model_validate(call["args"])
    refs = state.get("refs", {})
    verdicts = await judge(settings().judge_model, question(state), answer.sentences, refs)
    checks = []
    for s, v in zip(answer.sentences, verdicts, strict=True):
        unknown = [r for r in s.refs if r not in refs]
        problem = f"unknown refs {unknown}" if unknown else (v.reason if v.verdict == "unsupported" else None)
        checks.append({"sentence": s.text, "refs": s.refs, "verdict": v.verdict, "problem": problem})
    failed = [c for c in checks if c["problem"]]
    other = [ToolMessage("not run", tool_call_id=c["id"]) for c in state["messages"][-1].tool_calls if c is not call]
    if failed and state["rewrites"] < settings().rewrites:
        feedback = "Rejected. Fix or remove these sentences:\n" + "\n".join(
            f"- {c['sentence']} ({c['problem']})" for c in failed
        )
        audit(runtime, "answer_rejected", {"checks": checks})
        reply = [*other, ToolMessage(feedback, tool_call_id=call["id"])]
        return Command(goto="agent", update={"messages": reply, "rewrites": state["rewrites"] + 1})
    kept = [s for s, c in zip(answer.sentences, checks, strict=True) if not c["problem"]]
    fallback = FinalAnswer(
        sentences=[Sentence(text="I could not support an answer from the telemetry.", refs=[])], devices=[], count=None
    )
    final = answer.model_copy(update={"sentences": kept}) if kept else fallback
    audit(runtime, "answer", {"answer": final.model_dump(), "checks": checks, "withheld": len(failed)})
    text = " ".join(s.text + "".join(f" [{r}]" for r in s.refs) for s in final.sentences)
    update = {"messages": [*other, ToolMessage("Accepted.", tool_call_id=call["id"]), AIMessage(text)]}
    return Command(goto="approve", update=update | {"answer": final, "checks": checks})


async def approve(state: State, runtime: Runtime[Context]) -> dict:
    """Pause for a person on every proposal waiting for approval, then carry out the approved ones once."""
    waiting = [p for p in state.get("proposals", []) if p.status == "awaiting_approval"]
    if not waiting:
        return {}
    decisions: dict = interrupt({"proposals": [p.model_dump() for p in waiting]})
    c, updated = runtime.context, []
    for p in waiting:
        decision = decisions.get(p.id) or {}
        if decision.get("approve"):
            ref = c.store.execute(
                p.key, c.tenant.company_id, p.kind, p.target, p.args, c.actor, decision.get("note", "")
            )
            p = p.model_copy(update={"status": "executed", "reference": ref})
        else:
            p = p.model_copy(update={"status": "rejected", "reason": decision.get("note") or "rejected by reviewer"})
        audit(runtime, p.status, {"proposal": p.id, "kind": p.kind, "target": p.target, "reference": p.reference})
        updated.append(p)
    lines = [
        f"{p.id} {p.kind} for {p.target}: {p.status}" + (f" as {p.reference}" if p.reference else "") for p in updated
    ]
    return {"proposals": updated, "messages": [AIMessage("Review outcome. " + "; ".join(lines))]}


CHECKPOINTED = [
    ("app.tools", "Row"),
    ("app.actions", "Proposal"),
    ("app.graph", "Plan"),
    ("app.graph", "FinalAnswer"),
    ("app.graph", "Sentence"),
]


@asynccontextmanager
async def checkpoints(path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    """A SQLite checkpointer that reads back only the types this graph stores."""
    async with aiosqlite.connect(path) as conn:
        yield AsyncSqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINTED))


def build(checkpointer) -> StateGraph:
    """The compiled graph."""
    graph = StateGraph(State, context_schema=Context)
    graph.add_node("plan", plan)
    graph.add_node("agent", agent)
    graph.add_node("act", act)
    graph.add_node("verify", verify, destinations=("agent", "approve"))
    graph.add_node("approve", approve)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "agent")
    graph.add_conditional_edges("agent", route, ["act", "verify"])
    graph.add_edge("act", "agent")
    graph.add_edge("approve", END)
    return graph.compile(checkpointer=checkpointer)
