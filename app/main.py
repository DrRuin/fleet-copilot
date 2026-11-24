"""HTTP API and the web page. A session is bound to one company when it opens; nothing later can change that."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel

from app.config import ROOT, settings
from app.fleet import Fleet
from app.graph import Context, build, checkpoints
from app.store import Store


class Opened(BaseModel):
    """Who is signing in and for which company."""

    company: str
    actor: str


class Message(BaseModel):
    """One administrator message."""

    text: str


class Decision(BaseModel):
    """A reviewer's decision on one proposal."""

    approve: bool
    note: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the fleet, open the stores and compile the graph once."""
    s = settings()
    app.state.fleet = Fleet.load(s.data)
    app.state.store = Store(s.database)
    app.state.sessions: dict[str, Context] = {}
    app.state.busy: dict[str, asyncio.Lock] = {}
    async with checkpoints(s.checkpoints) as saver:
        app.state.graph = build(saver)
        yield


app = FastAPI(title="Fleet Copilot", lifespan=lifespan)


def session(session_id: str) -> Context:
    """The session's context, or 404 for an unknown id."""
    found = app.state.sessions.get(session_id)
    if found is None:
        raise HTTPException(404, "unknown session")
    return found


Session = Annotated[Context, Depends(session)]


@app.get("/api/companies")
async def companies() -> dict[str, int]:
    """Companies and their device counts, for the sign-in screen."""
    return {c: len(t.devices) for c, t in app.state.fleet.tenants.items()}


@app.post("/api/sessions")
async def open_session(body: Opened) -> dict:
    """Bind a new session to one company."""
    try:
        tenant = app.state.fleet.tenant(body.company)
    except KeyError:
        raise HTTPException(404, "unknown company") from None
    context = Context(tenant=tenant, store=app.state.store, session=uuid.uuid4().hex, actor=body.actor)
    app.state.sessions[context.session] = context
    app.state.busy[context.session] = asyncio.Lock()
    app.state.store.log(tenant.company_id, context.session, body.actor, "session", {})
    return {"session": context.session, "company": tenant.company_id, "devices": len(tenant.devices)}


def events(update: dict) -> list[ServerSentEvent]:
    """Translate one graph step into page events."""
    out = []
    for node, change in update.items():
        if node == "__interrupt__":
            out.append(ServerSentEvent(data=change[0].value, event="approval"))
        elif node == "plan":
            out.append(ServerSentEvent(data=change["plan"].model_dump(), event="plan"))
        elif node == "agent":
            calls = [{"name": c["name"], "args": c["args"]} for c in change["messages"][-1].tool_calls]
            out.append(ServerSentEvent(data={"calls": calls}, event="calls"))
        elif node == "act":
            rows = {k: v.model_dump() for k, v in change.get("refs", {}).items()}
            proposals = [p.model_dump() for p in change.get("proposals", [])]
            out.append(ServerSentEvent(data={"rows": rows, "proposals": proposals}, event="rows"))
        elif node in ("verify", "approve") and change:
            data = {k: change[k] for k in ("checks", "rewrites") if k in change}
            if "answer" in change:
                data["answer"] = change["answer"].model_dump()
            if "proposals" in change:
                data["proposals"] = [p.model_dump() for p in change["proposals"]]
            out.append(ServerSentEvent(data=data, event=node))
    return out


def idle(context: Session) -> asyncio.Lock:
    """The session's lock, or 409 while another request is running on it."""
    lock = app.state.busy[context.session]
    if lock.locked():
        raise HTTPException(409, "this session is still answering")
    return lock


Busy = Annotated[asyncio.Lock, Depends(idle)]


async def stream(context: Context, lock: asyncio.Lock, payload) -> AsyncIterator[ServerSentEvent]:
    """Run the graph for this session, one request at a time, and send each step as it finishes."""
    config = {"configurable": {"thread_id": context.session}}
    async with lock:
        try:
            async for update in app.state.graph.astream(payload, config, context=context, stream_mode="updates"):
                for event in events(update):
                    yield event
        except Exception as exc:  # noqa: BLE001
            context.store.log(context.tenant.company_id, context.session, context.actor, "error", {"error": str(exc)})
            message = "the copilot stopped with an error; the audit log shows anything already carried out"
            yield ServerSentEvent(data={"error": message}, event="error")
    yield ServerSentEvent(data={}, event="done")


@app.post("/api/sessions/{session_id}/messages", response_class=EventSourceResponse)
async def ask(context: Session, lock: Busy, body: Message) -> AsyncIterator[ServerSentEvent]:
    """Send a message and stream the agent's steps."""
    async for event in stream(context, lock, {"messages": [("user", body.text)]}):
        yield event


@app.post("/api/sessions/{session_id}/decisions", response_class=EventSourceResponse)
async def decide(context: Session, lock: Busy, body: dict[str, Decision]) -> AsyncIterator[ServerSentEvent]:
    """Answer the approval pause and stream what was carried out."""
    resume = Command(resume={k: v.model_dump() for k, v in body.items()})
    async for event in stream(context, lock, resume):
        yield event


@app.get("/api/sessions/{session_id}/snapshots/{snapshot_id}")
async def snapshot(context: Session, snapshot_id: str) -> dict:
    """A cited snapshot, only when it belongs to the session's company."""
    found = context.tenant.snapshot(snapshot_id)
    if found is None:
        raise HTTPException(404, "no such snapshot in this company")
    return found.model_dump(mode="json")


@app.get("/api/sessions/{session_id}/audit")
async def audit(context: Session) -> dict:
    """The company's audit log and executed actions."""
    company = context.tenant.company_id
    return {"events": context.store.audit(company), "actions": context.store.actions(company)}


app.mount("/", StaticFiles(directory=ROOT / "ui", html=True), name="ui")
