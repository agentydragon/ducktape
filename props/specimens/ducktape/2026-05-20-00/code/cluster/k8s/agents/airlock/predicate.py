from airlock.predicates import NeedsHumanDecision


def decide(server_namespace: str, tool_name: str, arguments: dict) -> NeedsHumanDecision:
    return NeedsHumanDecision()
