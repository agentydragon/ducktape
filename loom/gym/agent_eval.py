"""Run gym tasks as a live containerized agent eval: Inspect react agent in the wayback sandbox.

The model client runs host-side against the cluster LiteLLM (Anthropic-shaped
endpoint); the agent's tools execute in the Docker sandbox whose only network
route is the date-clamped wayback proxy. Point `--wayback-upstream` at the
in-cluster pull-through cache when the Docker daemon runs in-cluster
(docker-ci), e.g. `http://wayback-cache.wayback-cache.svc.cluster.local:8080`.

Requires two images in the local Docker daemon: the wayback proxy
(`bazelisk run //loom/wayback/proxy:load`) and the agent sandbox
(`docker build -t loom-gym-sandbox:latest loom/gym/sandbox/`).

    LITELLM_API_KEY=... bazelisk run //loom/gym:agent_eval_bin -- \\
        --model-id glm-4.5 --task-filter manifold-bitcoin-100k-2024
"""

from __future__ import annotations

import argparse
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inspect_ai import eval as inspect_eval
from inspect_ai.model import get_model

from finance.evidence.checkout import ensure_checkout
from loom.gym.inspect_harness import DEFAULT_WAYBACK_UPSTREAM, agent_eval_task
from loom.gym.monthly_series import load_series
from loom.gym.panel import build_panel
from loom.gym.series_tasks import admissible_tasks

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = "https://litellm.allegedly.works"
DEFAULT_LANGFUSE_TAGS = "loom-gym"


def _parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _default_eval_session_id(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"loom-gym-{timestamp}"


def _litellm_metadata(
    *,
    session_id: str,
    tags: list[str],
    model_id: str,
    endpoint_model: str,
    task_filter: str | None,
    archive: bool,
    wayback_upstream: str,
) -> dict[str, Any]:
    return {
        "trace_user_id": "loom-gym",
        "session_id": session_id,
        "tags": tags,
        "loom.eval.session_id": session_id,
        "loom.eval.model_id": model_id,
        "loom.eval.endpoint_model": endpoint_model,
        "loom.eval.task_filter": task_filter or "",
        "loom.eval.archive": archive,
        "loom.eval.wayback_upstream": wayback_upstream if archive else "",
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True, help="Must be present in KNOWN_MODEL_CUTOFFS.")
    parser.add_argument("--base-url", default=LITELLM_BASE_URL)
    parser.add_argument(
        "--endpoint-model", default=None, help="Model name the endpoint serves; default <model-id>-anthropic."
    )
    parser.add_argument("--api-key-env", default="LITELLM_API_KEY", help="Env var holding the API key.")
    parser.add_argument("--task-filter", default=None, help="Only run tasks whose id contains this substring.")
    parser.add_argument(
        "--panel", action="store_true", help="Run the curated non-redundant panel instead of the full grid."
    )
    parser.add_argument("--max-tasks", type=int, default=None, help="Cap the number of tasks (after filtering).")
    parser.add_argument("--wayback-upstream", default=DEFAULT_WAYBACK_UPSTREAM)
    parser.add_argument(
        "--wayback-upstream-auth-env",
        default="WAYBACK_UPSTREAM_AUTH",
        help="Env var holding the 'Bearer <token>' value for the authed cache route; empty if unset.",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Run the agent with no network at all (no wayback proxy); it forecasts from /data and its own "
        "knowledge. The --wayback-upstream* args are then unused.",
    )
    parser.add_argument("--log-dir", type=Path, required=True, help="Inspect eval log directory.")
    parser.add_argument("--message-limit", type=int, default=1000, help="Max conversation turns per sample.")
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="Max samples to run concurrently (one docker sandbox each). Bounds docker-ci "
        "network-pool usage; unset uses the Inspect default.",
    )
    parser.add_argument(
        "--langfuse-tags",
        default=DEFAULT_LANGFUSE_TAGS,
        help="Comma-separated tags attached to LiteLLM/Langfuse traces and the Inspect eval log.",
    )
    parser.add_argument(
        "--langfuse-session-id",
        default=None,
        help="Session id attached to LiteLLM/Langfuse traces; default is a generated loom-gym timestamp.",
    )
    parser.add_argument(
        "--compaction-threshold-tokens",
        type=int,
        default=0,
        help="Enable Inspect LLM-summary compaction above this input-token estimate; 0 disables compaction.",
    )
    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env) or Path("/tmp/litellm_key").read_text().strip()

    series = list(load_series(ensure_checkout()))
    tasks = admissible_tasks(series, model_id=args.model_id, task_filter=args.task_filter, strict=False)
    if args.panel:
        tasks = list(build_panel(tasks))
    if args.max_tasks is not None:
        tasks = tasks[: args.max_tasks]
    if not tasks:
        raise SystemExit(f"no admissible tasks ({args.model_id=}, {args.task_filter=}, {args.panel=})")
    mode = "no-archive" if args.no_archive else f"archive (upstream {args.wayback_upstream})"
    print(f"{len(tasks)} tasks for {args.model_id} via {args.base_url} [{mode}]")

    endpoint_model = args.endpoint_model or f"{args.model_id}-anthropic"
    langfuse_tags = _parse_csv(args.langfuse_tags)
    langfuse_session_id = args.langfuse_session_id or _default_eval_session_id()
    metadata = _litellm_metadata(
        session_id=langfuse_session_id,
        tags=langfuse_tags,
        model_id=args.model_id,
        endpoint_model=endpoint_model,
        task_filter=args.task_filter,
        archive=not args.no_archive,
        wayback_upstream=args.wayback_upstream,
    )
    print(f"litellm/langfuse session_id={langfuse_session_id} tags={','.join(langfuse_tags) or '-'}")

    model = get_model(
        f"anthropic/{endpoint_model}",
        base_url=args.base_url,
        api_key=api_key,
        # Anthropic Messages only permits a narrow `metadata` object. LiteLLM consumes
        # `litellm_metadata` before forwarding upstream, so Langfuse can still group
        # and filter these traces without sending invalid provider payloads.
        extra_body={"litellm_metadata": metadata},
    )
    logs = inspect_eval(
        agent_eval_task(
            tasks,
            series,
            wayback_upstream=args.wayback_upstream,
            wayback_upstream_auth=os.environ.get(args.wayback_upstream_auth_env, ""),
            archive=not args.no_archive,
            compaction_threshold_tokens=args.compaction_threshold_tokens or None,
        ),
        model=model,
        log_dir=str(args.log_dir),
        display="plain",
        tags=langfuse_tags,
        metadata={"litellm_metadata": metadata},
        message_limit=args.message_limit,
        max_samples=args.max_samples,
        # A transient per-sample failure (e.g. a flaky DNS/connection to the model
        # endpoint) should drop that sample, not abort the whole run.
        fail_on_error=False,
    )
    for log in logs:
        print(f"eval status={log.status}" + (f" error={log.error}" if log.error else ""))
        for sample in log.samples or []:
            score = (sample.scores or {}).get("gym_proper_loss")
            if score is None:
                print(f"{sample.id}: no score")
                continue
            meta = score.metadata or {}
            served = meta.get("served_evidence", [])
            fetched = " ".join(record["url"] for record in served) or "-"
            statuses = Counter(record["status"] for record in meta.get("upstream_errors", []))
            err_note = (
                " upstream_errors=" + ",".join(f"{s}×{n}" for s, n in sorted(statuses.items())) if statuses else ""
            )
            note = f" submission_error={meta['submission_error']}" if "submission_error" in meta else ""
            print(f"{sample.id}: value={score.value} answer={score.answer!r}{note} fetched=[{fetched}]{err_note}")


if __name__ == "__main__":
    main()
