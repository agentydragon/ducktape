"""Claude Code status line script.

Receives JSON on stdin, outputs formatted status to stdout.
Displays session info, model, cwd, cost, context window usage, and subscription
quota utilization. Quota data comes from `aiquota`'s shared cache (read +
populated via `QuotaService`), selecting the provider matching the running
session (claude vs z.ai), so the statusline agrees with the `aiquota` CLI and
GNOME extension.
"""

import asyncio
import logging
import os
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.text import Text

from aiquota.cache import QuotaService
from aiquota.config import DEFAULT_CONFIG_PATH, Config, load as load_config
from aiquota.models import ExtraSpend, FetchSuccess, ProviderQuota, QuotaWindow
from devinfra.claude.claude_api.credentials import read_credentials
from devinfra.claude.claude_api.statusline import ContextWindow, Input
from devinfra.claude.session_paths import default_cache_dir, hook_daemon_sock

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


def _format_extra_spend(extra: ExtraSpend) -> str:
    return f"extra ${extra.used_usd:.0f}/${extra.monthly_limit_usd:.0f} ({extra.utilization:.0f}%)"


def _quota_windows(pq: ProviderQuota) -> tuple[QuotaWindow | None, QuotaWindow | None, ExtraSpend | None, datetime]:
    """Return (short, long, extra, fetched_at), preferring the latest fetch and
    falling back to the last successful snapshot (mirroring aiquota's tmux renderer)."""
    result = pq.last_output.result
    if isinstance(result, FetchSuccess) and (result.short_window or result.long_window):
        return result.short_window, result.long_window, result.extra_spend, pq.last_output.fetched_at
    if pq.last_success is not None:
        succ = pq.last_success.result
        return succ.short_window, succ.long_window, succ.extra_spend, pq.last_success.fetched_at
    return None, None, None, pq.last_output.fetched_at


def _format_quota(quota: ProviderQuota | None, *, now: datetime) -> Text | None:
    if quota is None:
        return None
    short, long, extra, fetched_at = _quota_windows(quota)
    parts: list[str] = []
    if short is not None and short.used_percent >= 70:
        parts.append(f"5h:{short.used_percent:.0f}%")
    if long is not None:
        part = f"7d:{long.used_percent:.0f}%"
        remaining_s = long.reset_seconds
        if remaining_s > 0:
            remaining = timedelta(seconds=remaining_s)
            part += f" rst {_format_delta(remaining)}"
            # Project time until exhaustion based on burn rate.
            # Only show if projected to hit 100% before the window resets.
            util = long.used_percent
            if util > 0:
                elapsed_s = long.window_seconds - remaining_s
                if elapsed_s > 0:
                    time_to_exhaust = timedelta(seconds=(100 - util) / util * elapsed_s)
                    if time_to_exhaust < remaining:
                        part += f" dry {_format_delta(time_to_exhaust)}"
        parts.append(part)
    if extra is not None and extra.is_enabled:
        parts.append(_format_extra_spend(extra))
    if parts:
        age = now - fetched_at
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


def _is_zai_session(base_url: str) -> bool:
    """Detect a z.ai-backed `claude` session (the `z-claude` wrapper).

    z-claude points ANTHROPIC_BASE_URL at api.z.ai; the statusline then reports
    the z.ai provider's quota instead of Anthropic's.
    """
    if not base_url:
        return False
    host = urlparse(base_url).hostname or ""
    return host == "z.ai" or host.endswith(".z.ai")


def render(
    data: Input,
    *,
    is_subscription: bool,
    quota: ProviderQuota | None,
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

    quota_text = _format_quota(quota, now=now)
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

    # is_subscription still gates the per-session $cost display below.
    oauth = read_credentials()
    is_subscription = oauth is not None and oauth.subscription_type is not None

    # Quota comes from aiquota's shared cache (read + populated by fetch_all),
    # selecting the provider matching the running session.
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:
        logger.debug("Failed to load aiquota config, using defaults", exc_info=True)
        config = Config()
    service = QuotaService(config=config)
    quotas = asyncio.run(service.fetch_all())
    provider_name = "zai" if _is_zai_session(os.environ.get("ANTHROPIC_BASE_URL", "")) else "claude"
    quota = next((pq for pq in quotas.providers if pq.provider == provider_name), None)

    home_env = os.environ.get("HOME")
    output = render(
        data,
        is_subscription=is_subscription,
        quota=quota,
        home=Path(home_env) if home_env else None,
        now=datetime.now(UTC),
        daemon_healthy=_daemon_healthy(hook_daemon_sock(data.session_id)),
    )
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
