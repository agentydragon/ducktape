from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

from tana.litellm_proxy.model_registry import TANA_LLM_MODELS
from tana.litellm_proxy.provider import TanaProxyClient


@dataclass(frozen=True)
class ProbeResult:
    model_id: str
    ok: bool
    elapsed_seconds: float
    detail: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Tana llmProxy support for known RE-derived model IDs.")
    parser.add_argument("--model", action="append", help="Probe one model ID. Repeatable. Defaults to all models.")
    parser.add_argument("--max-tokens", type=int, default=1, help="Max output tokens for each probe.")
    parser.add_argument("--timeout-seconds", type=float, default=90.0, help="Per-model timeout.")
    parser.add_argument(
        "--continue-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep probing after a model fails.",
    )
    return parser.parse_args()


async def _probe_one(client: TanaProxyClient, model_id: str, max_tokens: int, timeout_seconds: float) -> ProbeResult:
    started = time.monotonic()
    messages = [{"role": "user", "content": "Reply with exactly: OK"}]
    try:
        response = await asyncio.wait_for(
            client.chat_completion(model_id, messages, {"temperature": 0, "max_tokens": max_tokens}),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        return ProbeResult(model_id=model_id, ok=False, elapsed_seconds=elapsed, detail=f"{type(exc).__name__}: {exc}")

    elapsed = time.monotonic() - started
    text = response.text.strip().replace("\n", " ")
    return ProbeResult(model_id=model_id, ok=True, elapsed_seconds=elapsed, detail=text[:120])


async def _main_async() -> int:
    args = _parse_args()
    requested = args.model or [model.model_id for model in TANA_LLM_MODELS]
    known_model_ids = {model.model_id for model in TANA_LLM_MODELS}
    unknown = [model_id for model_id in requested if model_id not in known_model_ids]
    if unknown:
        print(f"Unknown model IDs: {', '.join(unknown)}")
        return 2

    client = TanaProxyClient()
    failures = 0
    for model_id in requested:
        result = await _probe_one(client, model_id, args.max_tokens, args.timeout_seconds)
        status = "ok" if result.ok else "fail"
        print(f"{status}\t{result.elapsed_seconds:.1f}s\t{result.model_id}\t{result.detail}", flush=True)
        if not result.ok:
            failures += 1
            if not args.continue_on_error:
                break
    return 1 if failures else 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
