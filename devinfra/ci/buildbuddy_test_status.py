"""Publish an early GitHub status from BuildBuddy's live test target index."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Event
from typing import Any, cast

DEFAULT_BUILDBUDDY_URL = "https://app.buildbuddy.io"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
STATUS_CONTEXT = "BuildBuddy / Bazel tests (live)"
STOP = Event()


class APIError(Exception):
    """A BuildBuddy or GitHub API request failed."""


def _json_request(url: str, *, headers: dict[str, str], body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return cast(dict[str, Any], json.loads(response.read()))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise APIError(f"{url.split('?', maxsplit=1)[0]}: {error}") from error


def _buildbuddy_call(
    method: str, request: dict[str, Any], *, api_key: str, base_url: str = DEFAULT_BUILDBUDDY_URL
) -> dict[str, Any]:
    return _json_request(
        f"{base_url.rstrip('/')}/rpc/BuildBuddyService/{method}",
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": api_key},
        body=request,
    )


def _invocation(api_key: str, invocation_id: str, base_url: str) -> dict[str, Any] | None:
    response = _buildbuddy_call(
        "GetInvocation", {"lookup": {"invocationId": invocation_id}}, api_key=api_key, base_url=base_url
    )
    invocations = response.get("invocation") or []
    return invocations[0] if invocations else None


def _first_failed_test(api_key: str, invocation_id: str, base_url: str) -> str | None:
    # BuildBuddy reports a target as FAILED on its first non-passing TestResult,
    # before the target summary or whole invocation is complete.
    response = _buildbuddy_call(
        "GetTarget", {"invocationId": invocation_id, "status": "FAILED"}, api_key=api_key, base_url=base_url
    )
    for group in response.get("targetGroups") or []:
        for target in group.get("targets") or []:
            label = (target.get("metadata") or {}).get("label")
            if label:
                return str(label)
    return None


def _target_url(invocation_id: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/invocation/{urllib.parse.quote(invocation_id, safe='')}"


def _write_state(state_path: Path, state: str, description: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=state_path.parent, delete=False) as temp:
        json.dump({"state": state, "description": description}, temp)
        temp.write("\n")
        temp_path = Path(temp.name)
    temp_path.replace(state_path)


def _read_state(state_path: Path) -> dict[str, str] | None:
    try:
        value = json.loads(state_path.read_text())
    except OSError:
        return None
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("state"), str):
        return None
    return value


def _publish_status(
    *,
    repo: str,
    sha: str,
    token: str,
    invocation_id: str,
    state: str,
    description: str,
    state_path: Path,
    github_api_url: str = DEFAULT_GITHUB_API_URL,
    buildbuddy_url: str = DEFAULT_BUILDBUDDY_URL,
) -> None:
    url = (
        f"{github_api_url.rstrip('/')}/repos/{urllib.parse.quote(repo, safe='/')}"
        f"/statuses/{urllib.parse.quote(sha, safe='')}"
    )
    body = {
        "state": state,
        "target_url": _target_url(invocation_id, buildbuddy_url),
        "description": description[:140],
        "context": STATUS_CONTEXT,
    }
    try:
        _json_request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            body=body,
        )
    except APIError as error:
        # This status is additional feedback. The normal Test & Build job remains
        # authoritative, so an API hiccup should not interrupt CI.
        print(f"::warning::Could not publish live BuildBuddy status: {error}", file=sys.stderr, flush=True)
    _write_state(state_path, state, body["description"])


def _credentials() -> tuple[str, str, str]:
    api_key = os.environ.get("BUILDBUDDY_API_KEY", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    missing = [
        name
        for name, value in (
            ("BUILDBUDDY_API_KEY", api_key),
            ("GITHUB_TOKEN", github_token),
            ("GITHUB_REPOSITORY", repo),
        )
        if not value
    ]
    if missing:
        raise APIError(f"missing environment variables: {', '.join(missing)}")
    return api_key, github_token, repo


def _watch(args: argparse.Namespace) -> None:
    api_key, token, repo = _credentials()
    target_url = _target_url(args.invocation_id, args.buildbuddy_url)
    print(f"::notice::Watching BuildBuddy test invocation {args.invocation_id}: {target_url}", flush=True)
    while not STOP.is_set():
        try:
            invocation = _invocation(api_key, args.invocation_id, args.buildbuddy_url)
            if invocation is not None:
                failed_test = _first_failed_test(api_key, args.invocation_id, args.buildbuddy_url)
                if failed_test:
                    _publish_status(
                        repo=repo,
                        sha=args.sha,
                        token=token,
                        invocation_id=args.invocation_id,
                        state="failure",
                        description=f"First failing test: {failed_test}",
                        state_path=args.state_file,
                        github_api_url=args.github_api_url,
                        buildbuddy_url=args.buildbuddy_url,
                    )
                    return
                status = invocation.get("invocationStatus")
                if status == "COMPLETE_INVOCATION_STATUS":
                    success = bool(invocation.get("success"))
                    _publish_status(
                        repo=repo,
                        sha=args.sha,
                        token=token,
                        invocation_id=args.invocation_id,
                        state="success" if success else "failure",
                        description="Bazel test invocation passed" if success else "Bazel test invocation failed",
                        state_path=args.state_file,
                        github_api_url=args.github_api_url,
                        buildbuddy_url=args.buildbuddy_url,
                    )
                    return
                if status == "DISCONNECTED_INVOCATION_STATUS":
                    _publish_status(
                        repo=repo,
                        sha=args.sha,
                        token=token,
                        invocation_id=args.invocation_id,
                        state="error",
                        description="BuildBuddy test invocation disconnected",
                        state_path=args.state_file,
                        github_api_url=args.github_api_url,
                        buildbuddy_url=args.buildbuddy_url,
                    )
                    return
        except APIError as error:
            print(f"::warning::Live BuildBuddy test watcher will retry: {error}", file=sys.stderr, flush=True)
        STOP.wait(args.poll_interval)


def _stop_watcher(pid_file: Path) -> None:
    try:
        pid = int(pid_file.read_text().strip())
    except OSError:
        return
    except ValueError:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return
        if b"buildbuddy_test_status.py\0watch\0" in command:
            os.kill(pid, signal.SIGKILL)


def _finish(args: argparse.Namespace) -> None:
    _stop_watcher(args.pid_file)
    api_key, token, repo = _credentials()
    previous = _read_state(args.state_file)
    invocation = None
    api_failed = False
    deadline = time.monotonic() + (20 if args.remote_outcome == "success" else 0)
    while True:
        try:
            invocation = _invocation(api_key, args.invocation_id, args.buildbuddy_url)
        except APIError as error:
            api_failed = True
            print(f"::warning::Could not read final BuildBuddy invocation state: {error}", file=sys.stderr, flush=True)
            break
        if invocation is None or invocation.get("invocationStatus") != "PARTIAL_INVOCATION_STATUS":
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(2)

    if invocation is not None and invocation.get("invocationStatus") == "COMPLETE_INVOCATION_STATUS":
        success = bool(invocation.get("success"))
        state = "success" if success else "failure"
        description = "Bazel test invocation passed" if success else "Bazel test invocation failed"
        if not success and previous and previous.get("state") == "failure":
            description = previous.get("description", description)
    elif invocation is not None and invocation.get("invocationStatus") == "DISCONNECTED_INVOCATION_STATUS":
        state, description = "error", "BuildBuddy test invocation disconnected"
    elif previous and previous.get("state") == "failure":
        state, description = "failure", previous.get("description", "Bazel test invocation failed")
    elif invocation is None and not api_failed and args.remote_outcome == "success":
        state, description = "success", "No Bazel test invocation was needed"
    else:
        state, description = "error", "Bazel test result unavailable; see Test & Build job"

    # Avoid a duplicate status when the watcher already reported the terminal
    # outcome. A first-failure status keeps its target-specific description.
    if not previous or previous.get("state") != state:
        _publish_status(
            repo=repo,
            sha=args.sha,
            token=token,
            invocation_id=args.invocation_id,
            state=state,
            description=description,
            state_path=args.state_file,
            github_api_url=args.github_api_url,
            buildbuddy_url=args.buildbuddy_url,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("pending", "watch", "finish"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--invocation-id", required=True)
        sub.add_argument("--sha", required=True)
        sub.add_argument("--state-file", type=Path, required=True)
        sub.add_argument("--buildbuddy-url", default=os.environ.get("BUILDBUDDY_URL", DEFAULT_BUILDBUDDY_URL))
        sub.add_argument("--github-api-url", default=os.environ.get("GITHUB_API_URL", DEFAULT_GITHUB_API_URL))
        if command == "watch":
            sub.add_argument("--poll-interval", type=float, default=2.0)
        if command == "finish":
            sub.add_argument("--pid-file", type=Path, required=True)
            sub.add_argument("--remote-outcome", required=True, choices=["success", "failure", "cancelled", "skipped"])
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: STOP.set())
    if args.command == "pending":
        _, token, repo = _credentials()
        _publish_status(
            repo=repo,
            sha=args.sha,
            token=token,
            invocation_id=args.invocation_id,
            state="pending",
            description=f"Watching BuildBuddy invocation {args.invocation_id}",
            state_path=args.state_file,
            github_api_url=args.github_api_url,
            buildbuddy_url=args.buildbuddy_url,
        )
    elif args.command == "watch":
        _watch(args)
    else:
        _finish(args)


if __name__ == "__main__":
    try:
        main()
    except APIError as error:
        print(f"::warning::{error}", file=sys.stderr)
        sys.exit(0)
