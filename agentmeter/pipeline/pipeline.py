"""Phase 2 — minimal linear LangGraph pipeline (the test subject).

Builds a LangGraph StateGraph from the config-listed agents and wires them in a
strictly LINEAR chain: START -> a1 -> a2 -> ... -> an -> END. No conditional
edges, no loops, no governance. The agent list comes from config.pipeline.agents
so the harness is agent-count agnostic (3/4/5 agents all work). Agents named in
config.pipeline.custom are user-defined (pipeline/spec.py); the rest are the built-ins.
"""
from __future__ import annotations

from typing import Any, Callable, TypedDict, get_type_hints

from langgraph.graph import END, START, StateGraph

from .agents import AGENT_REGISTRY, make_custom_agent, make_verdict
from ..config import Config
from ..providers.base import ModelProvider


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
        self.custom: dict[str, dict] = dict(config.get("pipeline.custom", {}) or {})
        self._validate_agents()
        self.graph = self._build()

    def _validate_agents(self) -> None:
        if not self.agent_names:
            raise ValueError("pipeline.agents is empty in config.")
        unknown = [a for a in self.agent_names if a not in AGENT_REGISTRY and a not in self.custom]
        if unknown:
            raise ValueError(
                f"Unknown agent(s) in pipeline.agents: {unknown}. "
                f"Known: {sorted(AGENT_REGISTRY)}"
            )

    def _agent_fn(self, name: str):
        if name in self.custom:
            titles = {n: (self.custom.get(n) or {}).get("title") or n.capitalize() for n in self.agent_names}
            return make_custom_agent(name, self.custom[name], titles)
        return AGENT_REGISTRY[name]

    def _make_node(self, name: str):
        agent_fn = self._agent_fn(name)

        def node(state: dict) -> dict:
            update = self.node_hook(name, agent_fn, state, self.provider, self.config)
            update.pop("_parts", None)            # prompt parts: read by the instrumentation hook only
            return update

        return node

    def _build(self):
        state = PipelineState
        fields = get_type_hints(PipelineState)
        extra = [n for n in self.agent_names if n not in fields]
        if extra:   # custom agents: one text field each, beside the built-in ones
            state = TypedDict("PipelineState", {**fields, **{n: str for n in extra}}, total=False)
        builder = StateGraph(state)
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
        if "verdict" not in result:    # a pipeline without a verdict agent ends with its label
            result["verdict"] = make_verdict(result.get("predicted_class", "Unparseable"), "", self.config)
        return result
