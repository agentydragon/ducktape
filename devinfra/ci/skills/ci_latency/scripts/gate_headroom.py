"""Estimate conditional CI wall-clock headroom from per-head gate offsets.

Input is reviewed JSON with heads [{id, gates: {name: seconds}}] and scenarios
[{name, reductions_seconds: {gate: seconds}}]. Each head's gate offsets must
share the same labeled workflow-start proxy. Scenario reductions are conditional
subtractions from those offsets, not measured treatment effects.
"""

import argparse
import json
import math
from pathlib import Path


def _seconds(value, label):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{label} must be finite and nonnegative")
    return value


def _name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _last_gates(offsets):
    wall_seconds = max(offsets.values())
    return wall_seconds, sorted(gate for gate, seconds in offsets.items() if seconds == wall_seconds)


def _percentile_nearest_rank(values, percentile):
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def analyze(data):
    if not isinstance(data, dict):
        raise ValueError("input must be an object")
    heads = data.get("heads")
    scenarios = data.get("scenarios")
    if not isinstance(heads, list) or not heads:
        raise ValueError("heads must be a nonempty list")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("scenarios must be a nonempty list")

    parsed_heads = []
    head_ids = set()
    gate_names = None
    for index, head in enumerate(heads):
        label = f"heads[{index}]"
        if not isinstance(head, dict):
            raise ValueError(f"{label} must be an object")
        head_id = _name(head.get("id"), f"{label}.id")
        if head_id in head_ids:
            raise ValueError(f"duplicate head id: {head_id}")
        head_ids.add(head_id)
        gates = head.get("gates")
        if not isinstance(gates, dict) or not gates:
            raise ValueError(f"{label}.gates must be a nonempty object")
        parsed_gates = {
            _name(gate, f"{label}.gates key"): _seconds(seconds, f"{label}.gates[{gate!r}]")
            for gate, seconds in gates.items()
        }
        if gate_names is None:
            gate_names = set(parsed_gates)
        elif set(parsed_gates) != gate_names:
            raise ValueError("all heads must have the same complete gate set")
        parsed_heads.append({"id": head_id, "gates": parsed_gates})

    parsed_scenarios = []
    scenario_names = set()
    for index, scenario in enumerate(scenarios):
        label = f"scenarios[{index}]"
        if not isinstance(scenario, dict):
            raise ValueError(f"{label} must be an object")
        name = _name(scenario.get("name"), f"{label}.name")
        if name in scenario_names:
            raise ValueError(f"duplicate scenario name: {name}")
        scenario_names.add(name)
        reductions = scenario.get("reductions_seconds")
        if not isinstance(reductions, dict):
            raise ValueError(f"{label}.reductions_seconds must be an object")
        parsed_reductions = {}
        for gate, seconds in reductions.items():
            _name(gate, f"{label}.reductions_seconds key")
            if gate not in gate_names:
                raise ValueError(f"unknown gate in {label}.reductions_seconds: {gate}")
            parsed_reductions[gate] = _seconds(seconds, f"{label}.reductions_seconds[{gate!r}]")
        parsed_scenarios.append({"name": name, "reductions_seconds": parsed_reductions})

    baseline_heads = []
    last_gate_counts = dict.fromkeys(sorted(gate_names), 0)
    last_gate_tied_heads = 0
    for head in parsed_heads:
        wall_seconds, last_gates = _last_gates(head["gates"])
        if len(last_gates) > 1:
            last_gate_tied_heads += 1
        for gate in last_gates:
            last_gate_counts[gate] += 1
        descending = sorted(head["gates"].values(), reverse=True)
        second_highest = descending[1] if len(descending) > 1 else 0
        baseline_heads.append(
            {
                "id": head["id"],
                "wall_seconds": wall_seconds,
                "last_gates": last_gates,
                "gate_headroom_seconds": {
                    gate: max(0, seconds - second_highest) for gate, seconds in sorted(head["gates"].items())
                },
            }
        )

    scenario_results = []
    for scenario in parsed_scenarios:
        reductions = scenario["reductions_seconds"]
        savings = []
        results_by_head = []
        remaining_last_gate_counts = dict.fromkeys(sorted(gate_names), 0)
        remaining_last_gate_tied_heads = 0
        for head, baseline in zip(parsed_heads, baseline_heads, strict=True):
            adjusted = {gate: max(0, seconds - reductions.get(gate, 0)) for gate, seconds in head["gates"].items()}
            scenario_wall_seconds, last_gates = _last_gates(adjusted)
            saved_seconds = baseline["wall_seconds"] - scenario_wall_seconds
            savings.append(saved_seconds)
            if len(last_gates) > 1:
                remaining_last_gate_tied_heads += 1
            for gate in last_gates:
                remaining_last_gate_counts[gate] += 1
            results_by_head.append(
                {
                    "id": head["id"],
                    "baseline_wall_seconds": baseline["wall_seconds"],
                    "scenario_wall_seconds": scenario_wall_seconds,
                    "saved_wall_seconds": saved_seconds,
                    "last_gates": last_gates,
                }
            )
        scenario_results.append(
            {
                "name": scenario["name"],
                "mean_saved_wall_seconds": sum(savings) / len(savings),
                "p50_saved_wall_seconds": _percentile_nearest_rank(savings, 0.50),
                "p90_saved_wall_seconds": _percentile_nearest_rank(savings, 0.90),
                "total_saved_wall_seconds": sum(savings),
                "benefiting_heads": sum(saved > 0 for saved in savings),
                "remaining_last_gate_counts": remaining_last_gate_counts,
                "remaining_last_gate_tied_heads": remaining_last_gate_tied_heads,
                "heads": results_by_head,
            }
        )

    return {
        "population_n": len(parsed_heads),
        "clock": (
            "Gate offsets are elapsed seconds from each head's labeled workflow-start proxy, "
            "as supplied in the reviewed input."
        ),
        "assumption": (
            "Scenarios subtract the stated seconds from observed gate offsets, clipping each at zero, "
            "while holding other gate clocks and dependency structure fixed; adjusted completion order "
            "may change. This is a conditional upper-bound sensitivity, not a treatment effect or "
            "actual auto-merge readiness; "
            "do not add worker or CPU times."
        ),
        "last_gate_counts": last_gate_counts,
        "last_gate_tied_heads": last_gate_tied_heads,
        "heads": baseline_heads,
        "scenarios": scenario_results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        result = analyze(json.loads(args.input.read_text()))
    except (json.JSONDecodeError, OSError, ValueError) as error:
        parser.error(str(error))
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
