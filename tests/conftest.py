"""Shared fixtures: the real telemetry, a temp store, and a scripted model in place of OpenAI."""

from collections.abc import Callable

import pytest
from langchain.messages import AIMessage

from app import graph
from app.config import settings
from app.fleet import Fleet
from app.store import Store


@pytest.fixture(scope="session")
def fleet() -> Fleet:
    """The dataset, loaded once."""
    return Fleet.load(settings().data)


@pytest.fixture
def store(tmp_path) -> Store:
    """An empty audit store."""
    return Store(tmp_path / "copilot.sqlite")


class Scripted:
    """Stands in for ChatOpenAI: tool-calling turns come from a script, structured outputs from a function."""

    def __init__(self, turns: list[list[dict]], structured: Callable[[type, list], object]):
        """Hold the tool calls to make, one list per agent step, and the structured output maker."""
        self.turns, self.structured, self.schema = turns, structured, None

    def bind_tools(self, tools, **_) -> "Scripted":
        """Tools are ignored; the script decides."""
        return self

    def with_structured_output(self, schema, **_) -> "Scripted":
        """Remember the schema for the next call."""
        clone = Scripted(self.turns, self.structured)
        clone.schema = schema
        return clone

    async def ainvoke(self, messages, *_, **__):
        """The next scripted tool calls, or a structured output."""
        if self.schema is not None:
            return self.structured(self.schema, messages)
        calls = self.turns.pop(0)
        return AIMessage(
            "", tool_calls=[{"name": c["name"], "args": c["args"], "id": f"c{i}"} for i, c in enumerate(calls)]
        )


@pytest.fixture
def script(monkeypatch) -> Callable[[list[list[dict]], Callable], None]:
    """Replace the model with a script for one test."""

    def install(turns: list[list[dict]], structured: Callable) -> None:
        """Patch graph.chat to return the scripted model."""
        model = Scripted(turns, structured)
        monkeypatch.setattr(graph, "chat", lambda name: model)

    return install
