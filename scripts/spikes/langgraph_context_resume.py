"""Isolated LangGraph interrupt spike; never imported by Lumen production code.

Run with an ephemeral dependency instead of changing ``pyproject.toml``::

    uv run --with langgraph python scripts/spikes/langgraph_context_resume.py

The spike deliberately records node entries outside graph state. It proves
that the node containing ``interrupt`` starts again on resume, which is the
main semantic difference from Lumen's model-request-boundary clarification.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class State(TypedDict, total=False):
    request: str
    prepared: str
    answer: str
    committed: bool


node_entries: list[str] = []


def prepare(state: State) -> State:
    return {"prepared": f"prepared:{state['request']}"}


def agent(state: State) -> State:
    node_entries.append(state["prepared"])
    answer = interrupt({"question": "Choose A or B", "choices": ["A", "B"]})
    return {"answer": str(answer)}


def commit(state: State) -> State:
    assert state["answer"] == "A"
    return {"committed": True}


def build_graph():  # type: ignore[no-untyped-def]
    builder = StateGraph(State)
    builder.add_node("prepare", prepare)
    builder.add_node("agent", agent)
    builder.add_node("commit", commit)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "agent")
    builder.add_edge("agent", "commit")
    builder.add_edge("commit", END)
    return builder.compile(checkpointer=InMemorySaver())


def main() -> None:
    graph = build_graph()
    session_id = "lumen-session-spike"
    config = {"configurable": {"thread_id": session_id}}

    waiting = graph.invoke({"request": "demo"}, config=config)
    assert waiting["__interrupt__"][0].value["question"] == "Choose A or B"
    completed = graph.invoke(Command(resume="A"), config=config)

    assert completed["committed"] is True
    assert len(node_entries) == 2
    assert graph.get_state(config).config["configurable"]["thread_id"] == session_id
    print("spike passed: prepare -> agent -> interrupt -> resume -> commit")
    print("agent node entries:", len(node_entries), "(node restarts on resume)")


if __name__ == "__main__":
    main()
