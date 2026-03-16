"""Unified Claude Code hook entry point.

Reads JSON from stdin, parses into a discriminated union (AnyHookInput),
then dispatches to the appropriate handler via match/isinstance.
Initializes OTEL tracing from .claude_hooks/config.yaml if available.
Uses lazy imports for handler modules (mako, kubernetes, etc.) so lightweight
hooks like PreToolUse and PostToolUse don't pay for those imports.
"""

import asyncio
import logging
import sys
import traceback
from pathlib import Path

from opentelemetry import trace
from pydantic import BaseModel, TypeAdapter

from devinfra.claude import otel
from devinfra.claude.claude_api.hooks.dispatch_input import AnyHookInput
from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseInput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseInput
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookInput
from devinfra.claude.hook_config import HookConfig
from devinfra.claude.settings import HookSettings

logger = logging.getLogger(__name__)

_adapter: TypeAdapter[AnyHookInput] = TypeAdapter(AnyHookInput)
_tracer = trace.get_tracer(__name__)

# Claude Code stores per-session data at ~/.claude/session-env/<session_id>/
_SESSION_ENV_BASE = Path.home() / ".claude" / "session-env"


def _span_attrs(parsed: AnyHookInput) -> dict[str, str]:
    """Extract span attributes from a hook input model."""
    return {
        "hook.event_name": parsed.hook_event_name,
        "hook.session_id": parsed.session_id,
        "hook.cwd": str(parsed.cwd),
        "hook.input": parsed.model_dump_json(),
    }


def main() -> None:
    raw = sys.stdin.read()
    parsed = _adapter.validate_json(raw)
    cwd = Path(parsed.cwd)

    config = HookConfig.load_from_repo(cwd)
    if config and config.otel and config.otel.endpoint:
        otel.init_from_config(config.otel)

    session_dir = _SESSION_ENV_BASE / parsed.session_id

    with _tracer.start_as_current_span(f"hook/{parsed.hook_event_name}", attributes=_span_attrs(parsed)) as span:
        # TODO: Type output narrower than BaseModel (union of concrete output types).
        try:
            output: BaseModel
            match parsed:
                case SessionStartHookInput():
                    from devinfra.claude.session_start import _async_handle

                    settings = HookSettings(session_dir=session_dir)
                    output = asyncio.run(_async_handle(parsed, settings))

                case PreToolUseInput():
                    from devinfra.claude.pre_tool_use import evaluate as evaluate_pre

                    output = evaluate_pre(parsed)

                case PostToolUseInput():
                    from devinfra.claude.post_tool_use import evaluate as evaluate_post

                    output = evaluate_post(parsed)

                case _:
                    span.set_attribute("hook.handled", False)
                    return

            span.set_attribute("hook.handled", True)
            # exclude_none: Zod .optional() accepts undefined (absent) but NOT null.
            # Pydantic emits None as null by default; exclude_none omits those fields.
            # (exclude_unset would also drop Literal defaults like hookEventName.)
            sys.stdout.write(output.model_dump_json(by_alias=True, exclude_none=True))
        except Exception as e:
            span.set_status(trace.StatusCode.ERROR, str(e))
            span.record_exception(e)
            print(f"Hook failed ({parsed.hook_event_name}): {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        # Flush any buffered OTEL spans before exit.
        provider = trace.get_tracer_provider()
        if isinstance(provider, trace.ProxyTracerProvider):
            pass  # No real provider configured — nothing to flush.
        elif hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)
