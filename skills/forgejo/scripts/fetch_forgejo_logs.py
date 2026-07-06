#!/usr/bin/env python3
"""Fetch Forgejo Actions logs from the web UI endpoints.

Forgejo exposes run/task metadata through REST, but log retrieval on the
deployment this skill targets is driven by the web UI. This helper discovers
the current UI endpoint shape from the run page instead of hardcoding REST-like
IDs.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import os
import sys
import urllib.parse
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Any

import httpx


@dataclasses.dataclass(frozen=True)
class RunPageState:
    actions_url: str
    run_index: str
    job_index: str
    attempt: str
    initial_post_response: dict[str, Any]

    def log_endpoint(self, forgejo_url: str) -> str:
        base = forgejo_url.rstrip("/")
        actions = self.actions_url if self.actions_url.startswith("/") else "/" + self.actions_url
        return f"{base}{actions}/runs/{self.run_index}/jobs/{self.job_index}/attempt/{self.attempt}"


@dataclasses.dataclass(frozen=True)
class LogLine:
    timestamp: str | None
    message: str


class _ForgejoHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf: str | None = None
        self.run_attrs: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name: value or "" for name, value in attrs}
        if attr_map.get("name") == "_csrf" and "value" in attr_map:
            self.csrf = attr_map["value"]
        if "data-actions-url" in attr_map:
            self.run_attrs = attr_map


def parse_csrf(login_html: str) -> str:
    parser = _ForgejoHtmlParser()
    parser.feed(login_html)
    return parser.csrf or ""


def parse_run_page(run_html: str) -> RunPageState:
    parser = _ForgejoHtmlParser()
    parser.feed(run_html)
    attrs = parser.run_attrs
    if attrs is None:
        raise ValueError("run page did not contain a data-actions-url element")

    required = {
        "data-actions-url": "actions URL",
        "data-run-index": "run index",
        "data-job-index": "job index",
        "data-attempt-number": "attempt number",
        "data-initial-post-response": "initial post response",
    }
    missing = [label for key, label in required.items() if not attrs.get(key)]
    if missing:
        raise ValueError("run page is missing " + ", ".join(missing))

    initial_raw = html.unescape(attrs["data-initial-post-response"])
    try:
        initial = json.loads(initial_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("data-initial-post-response was not JSON") from exc

    if not isinstance(initial, dict):
        raise ValueError("data-initial-post-response JSON was not an object")

    return RunPageState(
        actions_url=attrs["data-actions-url"],
        run_index=attrs["data-run-index"],
        job_index=attrs["data-job-index"],
        attempt=attrs["data-attempt-number"],
        initial_post_response=initial,
    )


def _looks_like_steps(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    return all(isinstance(item, dict) for item in value) and any(
        any(key in item for key in ("index", "name", "status", "conclusion")) for item in value
    )


def _walk_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield child
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield child
            yield from _walk_values(child)


def extract_steps(initial_post_response: dict[str, Any]) -> list[dict[str, Any]]:
    state = initial_post_response.get("state")
    if isinstance(state, dict):
        current_job = state.get("currentJob")
        if isinstance(current_job, dict) and _looks_like_steps(current_job.get("steps")):
            return list(current_job["steps"])
    for value in _walk_values(initial_post_response):
        if _looks_like_steps(value):
            return list(value)
    return []


def build_log_payload(step: int) -> dict[str, Any]:
    return {"logCursors": [{"step": step, "cursor": None, "expanded": True}]}


def iter_log_lines(response: dict[str, Any]) -> Iterable[LogLine]:
    logs = response.get("logs")
    if not isinstance(logs, dict):
        return
    steps_log = logs.get("stepsLog")
    if not isinstance(steps_log, list):
        return
    for step_log in steps_log:
        if not isinstance(step_log, dict):
            continue
        lines = step_log.get("lines")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if isinstance(line, str):
                yield LogLine(timestamp=None, message=line)
            elif isinstance(line, dict):
                message = line.get("message", line.get("log", ""))
                yield LogLine(timestamp=line.get("timestamp") or line.get("time"), message=str(message))


def parse_log_response(response_text: str) -> list[LogLine]:
    parsed = json.loads(response_text)
    if not isinstance(parsed, dict):
        raise ValueError("log response JSON was not an object")
    return list(iter_log_lines(parsed))


def _response_text(response: httpx.Response) -> str:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400]
        raise RuntimeError(f"HTTP {exc.response.status_code} from {exc.request.url}: {body}") from exc
    return response.text


def login(forgejo_url: str, username: str, password: str, timeout: float) -> httpx.Client:
    login_url = f"{forgejo_url.rstrip('/')}/user/login"
    client = httpx.Client(follow_redirects=True, timeout=timeout)
    try:
        login_page = _response_text(client.get(login_url))
        _response_text(
            client.post(login_url, data={"_csrf": parse_csrf(login_page), "user_name": username, "password": password})
        )
    except Exception:
        client.close()
        raise
    return client


def fetch_run_page(client: httpx.Client, forgejo_url: str, owner: str, repo: str, run_number: str) -> str:
    path = "/".join(
        urllib.parse.quote(part.strip("/"), safe="") for part in (owner, repo, "actions", "runs", run_number)
    )
    return _response_text(client.get(f"{forgejo_url.rstrip('/')}/{path}"))


def fetch_step_logs(client: httpx.Client, forgejo_url: str, state: RunPageState, step: int) -> list[LogLine]:
    return parse_log_response(
        _response_text(client.post(state.log_endpoint(forgejo_url), json=build_log_payload(step)))
    )


def _step_index(step: dict[str, Any]) -> str:
    for key in ("index", "number", "step"):
        if key in step:
            return str(step[key])
    return "?"


def _step_name(step: dict[str, Any]) -> str:
    for key in ("name", "title", "displayName"):
        if key in step:
            return str(step[key])
    return ""


def _step_status(step: dict[str, Any]) -> str:
    for key in ("status", "conclusion", "state"):
        if key in step:
            return str(step[key])
    return ""


def _print_steps(steps: list[dict[str, Any]]) -> None:
    for step in steps:
        print("\t".join([_step_index(step), _step_status(step), _step_name(step)]))


def _print_logs(lines: list[LogLine], timestamps: bool) -> None:
    for line in lines:
        if timestamps:
            print("\t".join([line.timestamp or "", line.message]))
        else:
            print(line.message)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forgejo-url", default=os.environ.get("FORGEJO_URL"), help="Forgejo base URL")
    parser.add_argument("--owner", required=True, help="Repository owner")
    parser.add_argument("--repo", required=True, help="Repository name")
    parser.add_argument("--run", dest="run_number", required=True, help="UI run number, not REST run id")
    parser.add_argument("--user", default=os.environ.get("FORGEJO_USER"), help="Forgejo username")
    parser.add_argument(
        "--password",
        default=os.environ.get("FORGEJO_PASSWORD") or os.environ.get("FORGEJO_PASS"),
        help="Forgejo password; prefer FORGEJO_PASSWORD or FORGEJO_PASS over shell history",
    )
    parser.add_argument("--step", action="append", type=int, help="Step index to expand; repeatable")
    parser.add_argument("--list-steps", action="store_true", help="List step indexes from the run page")
    parser.add_argument("--timestamps", action="store_true", help="Print timestamp<TAB>message")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="Per-request timeout in seconds; applies to each attempted address"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.forgejo_url:
        raise SystemExit("--forgejo-url or FORGEJO_URL is required")
    if not args.user:
        raise SystemExit("--user or FORGEJO_USER is required")
    if not args.password:
        raise SystemExit("--password, FORGEJO_PASSWORD, or FORGEJO_PASS is required")

    client = login(args.forgejo_url, args.user, args.password, args.timeout)
    try:
        run_page = fetch_run_page(client, args.forgejo_url, args.owner, args.repo, args.run_number)
        state = parse_run_page(run_page)

        if args.list_steps or not args.step:
            _print_steps(extract_steps(state.initial_post_response))
            if not args.step:
                return 0

        for step in args.step:
            _print_logs(fetch_step_logs(client, args.forgejo_url, state, step), args.timestamps)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
