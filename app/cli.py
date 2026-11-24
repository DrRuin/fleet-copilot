"""Terminal chat with the copilot for one company: `uv run python -m app.cli acme-001`."""

import asyncio
import sys
import uuid

from langgraph.types import Command

from app.config import settings
from app.fleet import Fleet
from app.graph import Context, build, checkpoints
from app.store import Store


async def main(company: str) -> None:
    """Read questions from stdin, print answers, and ask for approval at each pause."""
    s = settings()
    tenant = Fleet.load(s.data).tenant(company)
    context = Context(tenant=tenant, store=Store(s.database), session=uuid.uuid4().hex, actor="cli")
    config = {"configurable": {"thread_id": context.session}}
    async with checkpoints(s.checkpoints) as saver:
        graph = build(saver)
        while (text := await asyncio.to_thread(input, f"\n{company}> ")).strip():
            result = await graph.ainvoke({"messages": [("user", text)]}, config, context=context)
            print(result["messages"][-1].content)
            while pauses := result.get("__interrupt__"):
                decisions = {}
                for p in pauses[0].value["proposals"]:
                    print(f"{p['id']} {p['kind']} {p['target']}: {p['args']}")
                    print(f"  facts: {p['facts']}")
                    yes = (await asyncio.to_thread(input, "  approve? [y/N] ")).strip().lower() == "y"
                    decisions[p["id"]] = {"approve": yes}
                result = await graph.ainvoke(Command(resume=decisions), config, context=context)
                print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "acme-001"))
