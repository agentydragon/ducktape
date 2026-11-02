"""Replay dashboard-owned Grafana datasource responses."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from playwright.async_api import Route

_VARIABLE = re.compile(r"\$(?:\{[^}]+\}|[a-zA-Z_][a-zA-Z0-9_]*)")


def _expression_pattern(expression: str, *, capture_variable: str | None = None) -> re.Pattern[str]:
    parts = []
    previous = 0
    captured = False
    for match in _VARIABLE.finditer(expression):
        parts.append(re.escape(expression[previous : match.start()]))
        if capture_variable and match.group() in {"$" + capture_variable, "${" + capture_variable + "}"}:
            parts.append("(?P=selected)" if captured else "(?P<selected>.*?)")
            captured = True
        else:
            parts.append(".*?")
        previous = match.end()
    parts.append(re.escape(expression[previous:]))
    return re.compile("".join(parts), re.DOTALL)


def _panels(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    panels = []
    for panel in items:
        panels.append(panel)
        panels.extend(_panels(panel.get("panels", [])))
    return panels


def validate_fixture(dashboard: dict[str, Any], fixture: dict[str, Any]) -> None:
    """Require an explicit response for every visible target, including lazy panels."""
    panels = fixture["panels"]
    variables = fixture.get("variables", {})
    for variable in dashboard.get("templating", {}).get("list", []):
        if variable.get("type") == "query" and variable["name"] not in variables:
            raise ValueError(f"missing recorded choices for dashboard variable {variable['name']}")
    for panel in _panels(dashboard.get("panels", [])):
        panel_id = str(panel.get("id"))
        for target in panel.get("targets", []):
            if target.get("hide"):
                continue
            ref = target.get("refId")
            if panel_id not in panels or ref not in panels[panel_id]:
                raise ValueError(f"missing recorded result for panel {panel_id}, query {ref}")
            result = panels[panel_id][ref]
            if "variants" in result:
                variable = result["variant_variable"]
                if variable not in variables or set(variables[variable]) - result["variants"].keys():
                    raise ValueError(f"missing recorded variants for panel {panel_id}, query {ref}")
                results = result["variants"].values()
            else:
                results = [result]
            if any(not isinstance(item.get("frames"), list) for item in results):
                raise ValueError(f"missing recorded frames for panel {panel_id}, query {ref}")


def recorded_result(query: dict[str, Any], fixture: dict[str, Any], dashboard: dict[str, Any]) -> dict[str, Any]:
    ref = query["refId"]
    panel_id = query.get("requestId", "").removesuffix(ref)
    panels: dict[str, dict[str, dict[str, Any]]] = fixture["panels"]

    def select(result: dict[str, Any], original: str) -> dict[str, Any]:
        variants = result.get("variants")
        if variants is None:
            return result
        variable = result["variant_variable"]
        match = _expression_pattern(original, capture_variable=variable).fullmatch(query.get("expr", ""))
        if match and match.groupdict().get("selected") in variants:
            selected: dict[str, Any] = variants[match.group("selected")]
            return selected
        raise KeyError(f"no recorded repeated-panel variant for panel {panel_id}, query {ref}")

    source_panels = _panels(dashboard.get("panels", []))
    if panel_id in panels and ref in panels[panel_id]:
        original = next(
            (
                target.get("expr", "")
                for panel in source_panels
                if str(panel.get("id")) == panel_id
                for target in panel.get("targets", [])
                if target.get("refId") == ref
            ),
            "",
        )
        return select(panels[panel_id][ref], original)

    # Repeated Grafana rows generate new panel IDs for each variable value.
    # Match their expanded query against the original panel's expression.
    expression = query.get("expr", "")
    matches: list[tuple[str, dict[str, Any]]] = []
    for panel in source_panels:
        key = str(panel.get("id"))
        if key not in panels or ref not in panels[key]:
            continue
        for target in panel.get("targets", []):
            original = target.get("expr")
            if target.get("refId") == ref and original and _expression_pattern(original).fullmatch(expression):
                matches.append((original, panels[key][ref]))
    if matches and all(original == matches[0][0] for original, _ in matches):
        return select(matches[0][1], matches[0][0])
    raise KeyError(f"no unambiguous recorded result for panel {panel_id}, query {ref}")


async def replay_query(
    route: Route, seen: asyncio.Event, fixture: dict[str, Any], dashboard: dict[str, Any], missing: list[str]
) -> None:
    payload = route.request.post_data_json
    assert payload is not None
    results = {}
    for query in payload["queries"]:
        try:
            results[query["refId"]] = recorded_result(query, fixture, dashboard)
        except KeyError as error:
            missing.append(str(error))
            await route.fulfill(status=400, json={"message": str(error)})
            seen.set()
            return
    await route.fulfill(json={"results": results})
    seen.set()
