"""Download census JSONL files from BuildBuddy's test-output artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def fetch(base_url: str, path: str, api_key: str) -> bytes:
    request = Request(f"{base_url}{path}", headers={"x-buildbuddy-api-key": api_key})
    with urlopen(request, timeout=120) as response:
        contents = response.read()
    if not isinstance(contents, bytes):
        raise TypeError("BuildBuddy response was not bytes")
    return contents


def safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.") or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("invocation_id")
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()

    api_key = os.environ.get("BUILDBUDDY_API_KEY")
    if not api_key:
        parser.error("BUILDBUDDY_API_KEY is required to download BuildBuddy test outputs")
    base_url = os.environ.get("BUILDBUDDY_URL", "https://app.buildbuddy.io").rstrip("/")
    events_query = urlencode({"invocation_id": args.invocation_id, "artifact": "raw_json"})
    events = json.loads(fetch(base_url, f"/file/download?{events_query}", api_key))

    downloaded = 0
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for event_index, event in enumerate(events):
        test_result = event.get("testResult")
        event_id = event.get("id", {}).get("testResult", {})
        if not isinstance(test_result, dict) or not isinstance(event_id, dict):
            continue
        for artifact in test_result.get("testActionOutput", []):
            if artifact.get("name") != "test.outputs/split_strategy_census.jsonl":
                continue
            uri = artifact.get("uri")
            if not isinstance(uri, str):
                continue
            query = urlencode({"invocation_id": args.invocation_id, "bytestream_url": uri})
            contents = fetch(base_url, f"/file/download?{query}", api_key)

            label = safe_component(event_id.get("label", "unknown-test"))
            suffix = [f"event_{event_index}"]
            suffix.extend(
                f"{field}_{event_id[field]}" for field in ("shard", "run", "attempt") if event_id.get(field) is not None
            )
            destination = args.output_directory / "__".join([label, *suffix]) / "split_strategy_census.jsonl"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
            downloaded += 1

    if not downloaded:
        raise ValueError(f"no split_strategy_census.jsonl test outputs found in invocation {args.invocation_id}")
    print(f"Downloaded {downloaded} census test output(s) from {args.invocation_id} to {args.output_directory}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
