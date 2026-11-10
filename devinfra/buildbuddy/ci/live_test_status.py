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

from devinfra.buildbuddy.cli import CLIError, run as bbapi

DEFAULT_BUILDBUDDY_URL = "https://app.buildbuddy.io"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
STATUS_CONTEXT = "BuildBuddy / Bazel ordinary tests (live)"
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


def _invocation(api_key: str, invocation_id: str, base_url: str) -> dict[str, Any] | None:
    try:
        response = json.loads(
            bbapi("invocation", "status", invocation_id, api_key=api_key, base_url=base_url, timeout=10)
        )
    except (CLIError, ValueError) as error:
        raise APIError(str(error)) from error
    invocations = response.get("invocation") or []
    return invocations[0] if invocations else None


def _first_failed_test(api_key: str, invocation_id: str, base_url: str) -> str | None:
    try:
        response = json.loads(
            bbapi("invocation", "first-failure", invocation_id, api_key=api_key, base_url=base_url, timeout=10)
        )
    except (CLIError, ValueError) as error:
        raise APIError(str(error)) from error
    return response.get("label") or None


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
    else:
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
    print(f"::notice::Watching BuildBuddy ordinary test invocation {args.invocation_id}: {target_url}", flush=True)
    announced_disconnect = False
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
                        description=f"First failing ordinary test: {failed_test}",
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
                        description="Bazel ordinary test invocation passed"
                        if success
                        else "Bazel ordinary test invocation failed",
                        state_path=args.state_file,
                        github_api_url=args.github_api_url,
                        buildbuddy_url=args.buildbuddy_url,
                    )
                    return
                # BuildBuddy can report DISCONNECTED when the build event stream
                # drops. This is not a terminal Bazel result; `finish` resolves it
                # from the Bazel step outcome.
                if status == "DISCONNECTED_INVOCATION_STATUS" and not announced_disconnect:
                    _publish_status(
                        repo=repo,
                        sha=args.sha,
                        token=token,
                        invocation_id=args.invocation_id,
                        state="pending",
                        description="Invocation disconnected; waiting for Bazel job result",
                        state_path=args.state_file,
                        github_api_url=args.github_api_url,
                        buildbuddy_url=args.buildbuddy_url,
                    )
                    announced_disconnect = True
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
        if b"devinfra.buildbuddy.ci.live_test_status\0watch\0" in command:
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
        description = "Bazel ordinary test invocation passed" if success else "Bazel ordinary test invocation failed"
        if not success and previous and previous.get("state") == "failure":
            description = previous.get("description", description)
    elif invocation is not None and invocation.get("invocationStatus") == "DISCONNECTED_INVOCATION_STATUS":
        # A disconnect is transient; the Bazel step outcome is authoritative.
        if previous and previous.get("state") == "failure":
            state, description = "failure", previous.get("description", "Bazel ordinary test invocation failed")
        elif args.remote_outcome == "success":
            state, description = "success", "Bazel CI passed (BuildBuddy invocation had disconnected)"
        elif args.remote_outcome == "failure":
            state, description = "failure", "Bazel CI failed (BuildBuddy invocation had disconnected)"
        else:
            state = "error"
            description = f"Bazel CI ended ({args.remote_outcome}) after BuildBuddy invocation disconnected"
    elif previous and previous.get("state") == "failure":
        state, description = "failure", previous.get("description", "Bazel ordinary test invocation failed")
    elif invocation is None and not api_failed and args.remote_outcome == "success":
        state, description = "success", "No Bazel ordinary test invocation was needed"
    elif invocation is None and not api_failed:
        state = "error"
        description = f"Bazel CI ended ({args.remote_outcome}) before starting ordinary tests; see Test & Build job"
    else:
        state, description = "error", "Bazel ordinary test result unavailable; see Test & Build job"

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
