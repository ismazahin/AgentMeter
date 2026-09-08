"""Phase 2 — minimal linear LangGraph pipeline (the test subject).

Builds a LangGraph StateGraph from the config-listed agents and wires them in a
strictly LINEAR chain: START -> a1 -> a2 -> ... -> an -> END. No conditional
edges, no loops, no governance. The agent list comes from config.pipeline.agents
so the harness is agent-count agnostic (3/4/5 agents all work).
"""
from __future__ import annotations

from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import AGENT_REGISTRY
from .config import Config
from .providers.base import ModelProvider


class PipelineState(TypedDict, total=False):
    scenario_id: str
    feature_prompt: str
    # per-agent textual outputs
    perceive: str
    reason: str
    decide: str
    act: str
    # decision artifacts
    predicted_class: str
    verdict: dict
    # transient: last GenerationResult (used by the instrumentation layer, Phase 3)
    _last_result: Any


# A node hook lets Phase 3 wrap each agent call with instrumentation without
# touching this builder. Default hook just runs the agent.
NodeHook = Callable[[str, Callable, dict, ModelProvider, Config], dict]


def _default_hook(name, agent_fn, state, provider, config) -> dict:
    return agent_fn(state, provider, config)


class Pipeline:
    def __init__(self, config: Config, provider: ModelProvider, node_hook: NodeHook | None = None):
        self.config = config
        self.provider = provider
        self.node_hook = node_hook or _default_hook
        self.agent_names: list[str] = list(config.get("pipeline.agents", []) or [])
        self._validate_agents()
        self.graph = self._build()

    def _validate_agents(self) -> None:
        if not self.agent_names:
            raise ValueError("pipeline.agents is empty in config.")
        unknown = [a for a in self.agent_names if a not in AGENT_REGISTRY]
        if unknown:
            raise ValueError(
                f"Unknown agent(s) in pipeline.agents: {unknown}. "
                f"Known: {sorted(AGENT_REGISTRY)}"
            )

    def _make_node(self, name: str):
        agent_fn = AGENT_REGISTRY[name]

        def node(state: dict) -> dict:
            return self.node_hook(name, agent_fn, state, self.provider, self.config)

        return node

    def _build(self):
        builder = StateGraph(PipelineState)
        for name in self.agent_names:
            builder.add_node(name, self._make_node(name))
        # strictly linear wiring
        builder.add_edge(START, self.agent_names[0])
        for a, b in zip(self.agent_names, self.agent_names[1:]):
            builder.add_edge(a, b)
        builder.add_edge(self.agent_names[-1], END)
        return builder.compile()

    def run(self, scenario_id: str, feature_prompt: str) -> PipelineState:
        initial: PipelineState = {
            "scenario_id": scenario_id,
            "feature_prompt": feature_prompt,
        }
        result = self.graph.invoke(initial)
        return result
