"""Load the pinned YAML releases installed into the k3s system-test stack."""

from pathlib import Path
from typing import Any

import yaml

from agentplane.system_tests.driver.tracing import tracer


def load_release_manifest(path: Path, name: str) -> list[dict[str, Any]]:
    with tracer.start_as_current_span("system_test.release_manifest.load", attributes={"manifest.name": name}) as span:
        source = path.read_text(encoding="utf-8")
        objects = [doc for doc in yaml.load_all(source, Loader=yaml.CSafeLoader) if doc]
        span.set_attribute("manifest.bytes", path.stat().st_size)
        span.set_attribute("manifest.documents", len(objects))
    return objects
