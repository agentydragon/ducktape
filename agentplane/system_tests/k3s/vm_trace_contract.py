"""Assertions over the VM suite's driver and Runner trace artifacts."""

from __future__ import annotations

import json
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    lines = path.read_bytes().splitlines(keepends=True)
    spans: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        text = line.decode("utf-8")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            incomplete_object = text.lstrip().startswith("{") and (
                error.pos == len(text) or error.msg.startswith("Unterminated string")
            )
            if index == len(lines) - 1 and not line.endswith(b"\n") and incomplete_object:
                break
            raise
        if not isinstance(value, dict):
            raise TypeError(f"trace artifact row must be an object: {path}")
        spans.append(value)
    return spans


def _attributes(span: dict[str, object]) -> dict[str, object]:
    value = span.get("attributes")
    return value if isinstance(value, dict) else {}


def _resource_uid(span: dict[str, object]) -> str | None:
    resource = span.get("resource")
    if not isinstance(resource, dict):
        return None
    uid = resource.get("sandbox.uid")
    return uid if isinstance(uid, str) and uid else None


def _trace_id(span: dict[str, object]) -> str | None:
    value = span.get("trace_id")
    if isinstance(value, str):
        return value
    context = span.get("context")
    if not isinstance(context, dict):
        return None
    value = context.get("trace_id")
    return value.removeprefix("0x") if isinstance(value, str) else None


def _sandbox_identities(spans: list[dict[str, object]], span_name: str) -> dict[str, str]:
    identities: dict[str, str] = {}
    for span in spans:
        attributes = _attributes(span)
        name = attributes.get("sandbox.name")
        uid = attributes.get("sandbox.uid")
        if span.get("name") == span_name and isinstance(name, str) and isinstance(uid, str) and uid:
            identities[name] = uid
    return identities


def _codex_session_uids(test_spans: list[dict[str, object]], scenario_ready: dict[str, str]) -> set[str]:
    uids: set[str] = set()
    for span in test_spans:
        if span.get("name") != "system_test.session.open":
            continue
        attributes = _attributes(span)
        name = attributes.get("sandbox.name")
        assert isinstance(name, str), "driver session.open has no sandbox.name"
        assert name, "driver session.open has no sandbox.name"
        assert name in scenario_ready, f"driver session.open Sandbox {name} has no ready identity"
        if attributes.get("harness") == "HARNESS_CODEX":
            uids.add(scenario_ready[name])
    return uids


def _assert_stages(
    stages_by_uid: dict[str, set[object]], identities: dict[str, str], required: set[str], role: str
) -> None:
    for name, uid in identities.items():
        missing = sorted(required - stages_by_uid.get(uid, set()))
        assert not missing, f"{role} Sandbox {name} ({uid}) is missing Runner stages: {missing}"


def assert_vm_process_trace_contract(output_dir: Path) -> None:
    test_spans = _read_jsonl(output_dir / "test-spans.jsonl")
    process_spans = _read_jsonl(output_dir / "process-spans.jsonl")
    scenario_ready = _sandbox_identities(test_spans, "system_test.sandbox.ready")
    assert scenario_ready, "no ready VM Sandbox spans reached test-spans.jsonl"

    runner_spans = [span for span in process_spans if span.get("service") == "agentplane-runner"]
    assert runner_spans, "no Runner spans reached process-spans.jsonl"
    unattributed = sorted({str(span.get("name")) for span in runner_spans if _resource_uid(span) is None})
    assert not unattributed, "Runner spans lack resource sandbox.uid: " + ", ".join(unattributed)
    stages_by_uid: dict[str, set[object]] = {}
    for span in runner_spans:
        if uid := _resource_uid(span):
            stages_by_uid.setdefault(uid, set()).add(span.get("name"))

    _assert_stages(
        stages_by_uid,
        scenario_ready,
        {
            "agentplane.runner.Runner/OpenSession",
            "Runner OpenSession",
            "agentplane.env.harnesshost.harness.start",
            "agentplane.env.harnesshost.harness.handshake",
        },
        "VM",
    )
    codex_uids = _codex_session_uids(test_spans, scenario_ready)
    assert codex_uids, "no ready Codex VM has a driver session.open span"
    codex_identities = {name: uid for name, uid in scenario_ready.items() if uid in codex_uids}
    _assert_stages(
        stages_by_uid,
        codex_identities,
        {
            "agentplane.codex.app_server.start",
            "agentplane.codex.app_server.launch",
            "agentplane.codex.app_server.initialize",
            "agentplane.codex.thread.handshake",
            "agentplane.codex.tool.start",
        },
        "Codex VM",
    )
    runner_open = [span for span in runner_spans if span.get("name") == "Runner OpenSession"]
    rpc_open_ids = {
        (_trace_id(span), span.get("span_id"))
        for span in runner_spans
        if span.get("name") == "agentplane.runner.Runner/OpenSession"
    }
    for span in runner_open:
        assert (_trace_id(span), span.get("parent_span_id")) in rpc_open_ids, (
            "Runner OpenSession did not retain its gRPC parent span"
        )
        name = _attributes(span).get("sandbox.name")
        if isinstance(name, str) and name in scenario_ready:
            assert _resource_uid(span) == scenario_ready[name], f"Runner OpenSession for {name} has wrong UID"

    for span in test_spans:
        if span.get("name") != "system_test.session.open":
            continue
        name = _attributes(span).get("sandbox.name")
        assert isinstance(name, str), "driver session.open has no sandbox.name"
        assert name in scenario_ready, f"driver session.open Sandbox {name} has no ready identity"
        assert any(
            _resource_uid(operation) == scenario_ready[name]
            and _attributes(operation).get("sandbox.name") == name
            and _trace_id(operation) == _trace_id(span)
            for operation in runner_open
        ), f"session.open trace for {name} did not reach its Runner operation span"
