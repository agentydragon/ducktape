"""Emit browser scenarios from the same Python view served by the API."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aiquota.testing.quota_fixtures import DEFAULT_NOW, FIXTURE_NAMES, load_quota_view_fixture
from util.bazel.runfiles import get_required_path


def scene_payloads(*, now: datetime = DEFAULT_NOW) -> dict[str, Any]:
    return {name: load_quota_view_fixture(_fixture_path(name), now=now) for name in FIXTURE_NAMES}


def _fixture_path(name: str) -> Path:
    return get_required_path(f"_main/aiquota/testing/fixtures/{name}.yaml")


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", type=Path, required=True, help="Where to write the /v1/quotas payload per scene")
    args = parser.parse_args()

    scenes = scene_payloads()
    _write(args.scenes, scenes)


if __name__ == "__main__":
    main()
