"""Replay the short, long-context, and synthetic tool-call model probes."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from util.bazel.runfiles import get_required_path, own_repo_rlocation

RUN_DIR = "cluster/docs/inference/runs/2026-09-24_qwen38_ssd"
CODING_REQUEST = f"{RUN_DIR}/coding_request.json"
LONG_CONTEXT_SOURCES = f"{RUN_DIR}/long_context_sources.json"
TOOL_ROUNDTRIP = f"{RUN_DIR}/dense_tool_roundtrip.json"
SOURCE_REVISION = "5883d2dc7297bd234ca640067e91136da2f45815"
TOKENIZE_PREFIX = (
    "The following repository source is background context for a prefill benchmark; it may end mid-file.\n<context>\n"
)
TOOL_RESULT = "verification_code=SSD-5090-7C2E"


def _read_json(runfile: str) -> Any:
    return json.loads(get_required_path(own_repo_rlocation(runfile)).read_text())


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _routes(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("--base-url must be an HTTP(S) URL without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("--base-url must not include a query or fragment")
    root = base_url.rstrip("/")
    api_base = root if parsed.path.rstrip("/").endswith("/v1") or parsed.path.rstrip("/") == "/v1" else f"{root}/v1"
    server_base = api_base[: -len("/v1")]
    return server_base, api_base


def _api_key(path: Path | None) -> str | None:
    if path is None:
        return None
    value = path.expanduser().read_text().strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("API key file must contain one non-empty line")
    return value


def _post_json(*, endpoint: str, value: Any, prefix: str, output: Path, api_key: str | None) -> Any:
    request_path = output / f"{prefix}.request.json"
    response_path = output / f"{prefix}.response.json"
    timing_path = output / f"{prefix}.timing.json"
    if any(path.exists() for path in (request_path, response_path, timing_path)):
        raise FileExistsError(f"probe output already exists under {output}; use a fresh --output-dir")

    request_body = _json_bytes(value)
    request_path.write_bytes(request_body)
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(endpoint, data=request_body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        response_body = error.read()
    except urllib.error.URLError:
        timing_path.write_bytes(_json_bytes({"http_status": None, "server_timings": None, "usage": None}))
        raise RuntimeError(f"request to {urlsplit(endpoint).path} failed before receiving an HTTP response") from None
    response_path.write_bytes(response_body)
    try:
        response_json = json.loads(response_body)
    except json.JSONDecodeError:
        response_json = None
    timing_path.write_bytes(
        _json_bytes(
            {
                "http_status": status,
                "server_timings": response_json.get("timings") if isinstance(response_json, dict) else None,
                "usage": response_json.get("usage") if isinstance(response_json, dict) else None,
            }
        )
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"request to {urlsplit(endpoint).path} returned HTTP {status}; response was saved")
    if response_json is None:
        raise RuntimeError(f"request to {urlsplit(endpoint).path} returned non-JSON; response was saved")
    return response_json


def _set_model(request: dict[str, Any], model: str | None) -> dict[str, Any]:
    result = copy.deepcopy(request)
    if model is not None:
        result["model"] = model
    return result


def _source_context(workspace: Path, manifest: dict[str, Any]) -> str:
    chunks = []
    for item in manifest["files"]:
        path = item["path"]
        try:
            source = subprocess.run(
                ["git", "show", f"{SOURCE_REVISION}:{path}"],
                cwd=workspace,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ).stdout
        except FileNotFoundError, subprocess.CalledProcessError:
            raise RuntimeError(f"could not read {path} from pinned source revision {SOURCE_REVISION}") from None
        actual_hash = hashlib.sha256(source).hexdigest()
        if actual_hash != item["sha256"]:
            raise ValueError(f"source hash does not match long_context_sources.json: {path}")
        chunks.append(f"\n--- {path} ---\n{source.decode('utf-8')}")
    return "".join(chunks)


def _long_context_request(
    *,
    server_base: str,
    base_request: dict[str, Any],
    sources: dict[str, Any],
    workspace: Path,
    output: Path,
    api_key: str | None,
) -> dict[str, Any]:
    source_text = _source_context(workspace, sources)
    tokenized = _post_json(
        endpoint=f"{server_base}/tokenize",
        value={"content": source_text},
        prefix="long_context.tokenize",
        output=output,
        api_key=api_key,
    )
    tokens = tokenized.get("tokens")
    count = sources["context_tokens"]
    if not isinstance(tokens, list) or any(type(token) is not int for token in tokens) or len(tokens) < count:
        raise RuntimeError(f"/tokenize returned fewer than {count} source token IDs")
    selected_tokens = tokens[:count]
    detokenized = _post_json(
        endpoint=f"{server_base}/detokenize",
        value={"tokens": selected_tokens},
        prefix="long_context.detokenize",
        output=output,
        api_key=api_key,
    )
    content = detokenized.get("content")
    if not isinstance(content, str):
        raise RuntimeError("/detokenize response did not contain string content")
    prompt = f"{TOKENIZE_PREFIX}{content}\n</context>\nTask:\n{base_request['messages'][0]['content']}"
    request = copy.deepcopy(base_request)
    request["messages"] = [{"role": "user", "content": prompt}]
    actual_hash = hashlib.sha256(_json_bytes(request)).hexdigest()
    if actual_hash != sources["request_sha256"]:
        raise ValueError(
            "reconstructed long-context request does not match its recorded SHA-256 "
            f"(expected {sources['request_sha256']}, got {actual_hash})"
        )
    return request


def _probe_short(api_base: str, output: Path, api_key: str | None, model: str | None) -> None:
    request = _set_model(_read_json(CODING_REQUEST), model)
    _post_json(
        endpoint=f"{api_base}/chat/completions", value=request, prefix="short_coding", output=output, api_key=api_key
    )


def _probe_long(*, server_base: str, api_base: str, output: Path, api_key: str | None, model: str | None) -> None:
    base_request = _read_json(CODING_REQUEST)
    sources = _read_json(LONG_CONTEXT_SOURCES)
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace is None:
        raise RuntimeError("run this probe with bazelisk run so the pinned git source tree is available")
    request = _long_context_request(
        server_base=server_base,
        base_request=base_request,
        sources=sources,
        workspace=Path(workspace),
        output=output,
        api_key=api_key,
    )
    request = _set_model(request, model)
    _post_json(
        endpoint=f"{api_base}/chat/completions", value=request, prefix="long_context", output=output, api_key=api_key
    )


def _probe_tools(api_base: str, output: Path, api_key: str | None, model: str | None) -> None:
    captured = _read_json(TOOL_ROUNDTRIP)
    request = _set_model(captured[0]["request"], model)
    first = _post_json(
        endpoint=f"{api_base}/chat/completions", value=request, prefix="tools.turn1", output=output, api_key=api_key
    )
    message = first["choices"][0]["message"]
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise RuntimeError("first tool probe response did not contain exactly one tool call")
    call = calls[0]
    function = call["function"]
    try:
        arguments = json.loads(function["arguments"])
    except KeyError, TypeError, json.JSONDecodeError:
        raise RuntimeError("first tool probe response contained invalid tool arguments") from None
    if function.get("name") != "read_file" or arguments != {"path": "sanity.txt"} or not call.get("id"):
        raise RuntimeError("first tool probe response did not request the expected synthetic read_file call")

    second_request = copy.deepcopy(request)
    second_request["messages"] = [
        *second_request["messages"],
        message,
        {"role": "tool", "tool_call_id": call["id"], "name": "read_file", "content": TOOL_RESULT},
    ]
    _post_json(
        endpoint=f"{api_base}/chat/completions",
        value=second_request,
        prefix="tools.turn2",
        output=output,
        api_key=api_key,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="llama.cpp server root, optionally ending in /v1")
    parser.add_argument("--model", help="override the model ID in the captured request")
    parser.add_argument("--api-key-file", type=Path, help="file containing a Bearer API key; omitted for no auth")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="directory for this probe's exact request/response captures"
    )
    parser.add_argument("--selector", choices=("short", "long", "tools"), required=True)
    args = parser.parse_args()

    server_base, api_base = _routes(args.base_url)
    output = args.output_dir.expanduser().resolve() / args.selector
    output.mkdir(parents=True, exist_ok=True)
    api_key = _api_key(args.api_key_file)
    match args.selector:
        case "short":
            _probe_short(api_base, output, api_key, args.model)
        case "long":
            _probe_long(server_base=server_base, api_base=api_base, output=output, api_key=api_key, model=args.model)
        case "tools":
            _probe_tools(api_base, output, api_key, args.model)
    print(f"probe captures saved under {output}")


if __name__ == "__main__":
    main()
