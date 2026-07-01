"""Claude Code status line script.

Receives JSON on stdin, outputs formatted status to stdout.
Displays session info, model, cwd, cost, context window usage,
and subscription quota utilization.
"""

import logging
import os
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.text import Text

from devinfra.claude.claude_api.credentials import read_credentials
from devinfra.claude.claude_api.statusline import ContextWindow, Input
from devinfra.claude.claude_api.usage import ExtraUsageTotals, normalized_extra_usage
from devinfra.claude.session_paths import default_cache_dir, hook_daemon_sock
from devinfra.claude.statusline.usage_cache import CachedUsage, UsageCache

_STALE_THRESHOLD = timedelta(seconds=10)
_SEP = Text(" ")

logger = logging.getLogger(__name__)


def _format_delta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds >= 86400:
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        return f"{days}d{hours:02d}h"
    if total_seconds >= 3600:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        return f"{hours}h{minutes:02d}m"
    if total_seconds >= 60:
        return f"{total_seconds // 60}m"
    return f"{total_seconds}s"


def _format_extra_usage(extra: ExtraUsageTotals) -> str:
    used = extra.used_credits / 100
    limit = extra.monthly_limit / 100
    pct = extra.utilization
    return f"extra ${used:.0f}/${limit:.0f} ({pct:.0f}%)"


def _format_quota(cached: CachedUsage | None, now: datetime) -> Text | None:
    if cached is None:
        return None
    usage = cached.usage
    parts: list[str] = []
    if usage.five_hour is not None and usage.five_hour.utilization >= 70:
        parts.append(f"5h:{usage.five_hour.utilization:.0f}%")
    if usage.seven_day is not None:
        part = f"7d:{usage.seven_day.utilization:.0f}%"
        if usage.seven_day.resets_at is not None:
            remaining = usage.seven_day.resets_at - now
            if remaining.total_seconds() > 0:
                part += f" rst {_format_delta(remaining)}"
                # Project time until exhaustion based on burn rate.
                # Only show if projected to hit 100% before the window resets.
                util = usage.seven_day.utilization
                if util > 0:
                    elapsed = timedelta(days=7) - remaining
                    elapsed_s = elapsed.total_seconds()
                    if elapsed_s > 0:
                        time_to_exhaust = timedelta(seconds=(100 - util) / util * elapsed_s)
                        if time_to_exhaust < remaining:
                            part += f" dry {_format_delta(time_to_exhaust)}"
        parts.append(part)
    extra = normalized_extra_usage(usage)
    if extra is not None:
        parts.append(_format_extra_usage(extra))
    if parts:
        age = now - cached.fetched_at
        if age > _STALE_THRESHOLD:
            parts.append(f"({_format_delta(age)} ago)")
    if not parts:
        return None
    return Text(" ".join(parts), style="dim")


def _format_context(ctx: ContextWindow | None) -> Text | None:
    if ctx is None or ctx.used_percentage is None:
        return None
    pct = ctx.used_percentage
    if pct >= 90:
        style = "bold red"
    elif pct >= 60:
        style = "yellow"
    else:
        style = "green"
    return Text(f"ctx:{pct:.0f}%", style=style)


def _format_daemon(healthy: bool) -> Text:
    if healthy:
        return Text("daemon ✓", style="green")
    return Text("daemon ✗", style="red")


def _daemon_healthy(sock_path: Path, timeout: float = 0.5) -> bool:
    """Check the Rust hook daemon health endpoint without importing daemon code."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            status_line = sock.recv(128).split(b"\r\n", 1)[0]
            return status_line.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200"))
    except OSError:
        return False


def render(
    data: Input,
    *,
    is_subscription: bool,
    cached_usage: CachedUsage | None,
    home: Path | None,
    now: datetime,
    daemon_healthy: bool,
) -> str:
    """Render the statusline as a plain string."""
    model_name = (data.model.display_name or data.model.id) if data.model else "unknown"

    cwd = ""
    if data.workspace:
        cwd = data.workspace.current_dir
    elif data.cwd:
        cwd = data.cwd

    if home is not None:
        home_str = str(home)
        if cwd.startswith(home_str):
            cwd = "~" + cwd[len(home_str) :]

    # Hide per-session cost for subscription users (it's meaningless).
    # TODO: Wire up Admin API (/v1/organizations/cost_report) with a read-only
    # admin key to show current-month API cost in the statusline.
    if is_subscription:
        segments: list[Text] = [Text(f"{model_name} {cwd}")]
    else:
        cost = data.cost.total_cost_usd if data.cost else 0.0
        segments = [Text(f"{model_name} {cwd} ${cost:.2f}")]

    context_text = _format_context(data.context_window)
    if context_text is not None:
        segments.append(context_text)

    quota_text = _format_quota(cached_usage, now=now)
    if quota_text is not None:
        segments.append(quota_text)

    segments.append(_format_daemon(daemon_healthy))

    console = Console(highlight=False, file=None, force_terminal=True, width=500)
    with console.capture() as capture:
        console.print(_SEP.join(segments), end="")
    return capture.get()


def main() -> None:
    raw = sys.stdin.read()

    try:
        log_path = default_cache_dir() / "statusline.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(raw + "\n")
    except OSError:
        pass

    data = Input.model_validate_json(raw)

    oauth = read_credentials()
    is_subscription = oauth is not None and oauth.subscription_type is not None
    access_token = oauth.access_token if oauth else None
    usage_cache = UsageCache(path=default_cache_dir() / "usage_cache.json")

    home_env = os.environ.get("HOME")
    output = render(
        data,
        is_subscription=is_subscription,
        cached_usage=usage_cache.get(access_token),
        home=Path(home_env) if home_env else None,
        now=datetime.now(UTC),
        daemon_healthy=_daemon_healthy(hook_daemon_sock(data.session_id)),
    )
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
