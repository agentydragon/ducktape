"""Concurrent scenario scopes keep OTel parentage and export failures."""

import asyncio
import json

import pytest
import pytest_bazel
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import StatusCode

from agentplane.system_tests import tracing
from util.otel import JsonlSpanExporter
from util.testing.undeclared_outputs import undeclared_outputs_dir


async def test_concurrent_scenarios_keep_parentage_and_record_failures() -> None:
    path = undeclared_outputs_dir() / "scenario-tracing-test.jsonl"
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(path)))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tracing, "tracer", provider.get_tracer(__name__))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked() -> None:
            entered.set()
            await release.wait()

        async def failed() -> None:
            await entered.wait()
            raise ValueError("scenario refused")

        with tracing.tracer.start_as_current_span("test-parent") as parent:
            parent_id = f"0x{parent.get_span_context().span_id:016x}"
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(tracing.traced_scenario("blocked", blocked()))
                with pytest.raises(ValueError, match="scenario refused"):
                    await tracing.traced_scenario("failed", failed())
                release.set()
        provider.shutdown()

    spans = [json.loads(line) for line in path.read_text().splitlines()]
    children = {span["attributes"]["scenario"]: span for span in spans if span["name"] == "system_test.scenario"}
    assert set(children) == {"blocked", "failed"}
    assert all(span["parent_id"] == parent_id for span in children.values())
    assert children["failed"]["status"]["status_code"] == StatusCode.ERROR.name
    assert children["blocked"]["status"]["status_code"] == StatusCode.UNSET.name
    summary = tracing.timing_summary(path)
    assert "system_test.scenario [blocked]" in summary
    assert "system_test.scenario [failed]" in summary
    assert "test-parent" not in summary


if __name__ == "__main__":
    pytest_bazel.main()
