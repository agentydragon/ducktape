from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi.openapi.utils import get_openapi

from rspcache.admin_app import ADMIN_APP

OPENAI_SPEC_URL = "https://raw.githubusercontent.com/openai/openai-openapi/2025-03-21/openapi.yaml"


def build_admin_schema() -> dict[str, Any]:
    return get_openapi(
        title=ADMIN_APP.title,
        version="1.0.0",
        description=ADMIN_APP.description or "rspcache admin API",
        routes=ADMIN_APP.routes,
    )


def fetch_openai_schema() -> dict[str, Any]:
    with httpx.Client(timeout=30) as client:
        resp = client.get(OPENAI_SPEC_URL)
        resp.raise_for_status()
        data = yaml.safe_load(resp.text)
    if not isinstance(data, dict):
        raise TypeError("OpenAI OpenAPI document is not a JSON object")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate OpenAPI schemas for rspcache tooling.")
    parser.add_argument(
        "--admin-output",
        type=Path,
        default=Path("adgn/rspcache_admin_ui/src/generated/admin-openapi.json"),
        help="Path to write the admin OpenAPI schema JSON.",
    )
    parser.add_argument(
        "--openai-output",
        type=Path,
        default=Path("adgn/rspcache_admin_ui/src/generated/openai-openapi.json"),
        help="Path to write the upstream OpenAI OpenAPI schema JSON.",
    )
    args = parser.parse_args()

    write_json(args.admin_output, build_admin_schema())
    write_json(args.openai_output, fetch_openai_schema())


if __name__ == "__main__":
    main()
