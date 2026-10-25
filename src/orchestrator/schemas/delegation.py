"""Delegation rules shared by the model runtime and authoritative Dispatcher."""

from .task_graph import Agent, TaskGraphOutput


ANALYZE_TARGETS = frozenset({Agent.COMMUNICATOR, Agent.PLAN_DELEGATE})


class InvalidDelegation(ValueError):
    pass


def validate_delegation(graph: TaskGraphOutput) -> None:
    if graph.producer_agent != Agent.ANALYZE:
        return
    if any(node.target_agent not in ANALYZE_TARGETS for node in graph.nodes):
        raise InvalidDelegation("ANALYZE may delegate only to COMMUNICATOR or PLAN_DELEGATE")
    if graph.human_request or any(node.node_type == "HUMAN_APPROVAL" for node in graph.nodes):
        raise InvalidDelegation("ANALYZE must ask for clarification through a COMMUNICATOR TASK, not directly through human_request/HUMAN_APPROVAL")


def decision_schema(agent: str) -> dict:
    schema = TaskGraphOutput.model_json_schema()
    # Decision passes always finish a claimed task. Only synthetic human intake
    # may omit a result in the broader transport protocol.
    schema["properties"]["source_result"] = {"$ref": "#/$defs/SourceResult"}
    schema["required"] = [*schema["required"], "source_result"]
    if agent == Agent.COMMUNICATOR:
        result = schema["$defs"]["SourceResult"]["properties"]
        result["outcome"] = {"type": "string", "enum": ["SUCCESS"]}
        result["proposed_status"] = {"type": "string", "enum": ["DONE"], "default": "DONE"}
    if agent == Agent.ANALYZE:
        # Restrict node targets only: producer_agent must still accept ANALYZE.
        node = schema["$defs"]["GraphNode"]["properties"]
        node["target_agent"] = {"type": "string", "enum": sorted(ANALYZE_TARGETS)}
        node["node_type"]["enum"] = ["TASK", "NOTIFICATION"]
        schema["properties"]["human_request"] = {"type": "null", "default": None}
    return schema
