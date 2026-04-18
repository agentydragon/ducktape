"""CLI for running Grocy MCP eval cases against a fresh, temporary Grocy container.

Each case (see `cases.py`) runs in its own container so the initial state
one case seeds can't leak into the next. Output lands under
`<output-dir>/<case-id>/` with transcript, summary, and final_state.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from x.grocy_mcp.eval.cases import CASES, EvalCase
from x.grocy_mcp.eval.result_types import EvalResult
from x.grocy_mcp.eval.run import DEFAULT_MODELS, run_grocy_eval
from x.grocy_mcp.grocy_container import grocy_url, run_grocy_container


async def _run_case(*, case: EvalCase, api: str, model: str, output_dir: Path, base_url: str | None) -> EvalResult:
    case_dir = output_dir / case.id
    # Don't bind-mount the DB: on gvisor sandboxes bind-mount propagation of
    # Grocy's config.php creation is racy (see grocy_container.py). Final
    # state is captured via REST API into final_state.json, so the SQLite DB
    # is ephemeral.
    with run_grocy_container() as container:
        return await run_grocy_eval(
            case=case, api=api, model=model, grocy_base_url=grocy_url(container), output_dir=case_dir, base_url=base_url
        )


async def _run_all(
    *, cases: list[EvalCase], api: str, model: str, output_dir: Path, base_url: str | None
) -> list[EvalResult]:
    results: list[EvalResult] = []
    for case in cases:
        print(f"\n=== Case: {case.id} ===")
        results.append(await _run_case(case=case, api=api, model=model, output_dir=output_dir, base_url=base_url))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grocy MCP eval — spin up a fresh Grocy container per case, run an agent against it, record the rollout"
    )
    parser.add_argument(
        "--case",
        choices=sorted(CASES),
        action="append",
        help="Case id(s) to run (default: all). Pass --case multiple times to select a subset.",
    )
    parser.add_argument("--api", choices=["openai", "anthropic"], default="openai", help="LLM API provider")
    parser.add_argument("--model", default=None, help="Model name (default depends on --api)")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: eval_results/<timestamp>)")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL (for Ollama/LiteLLM)")
    args = parser.parse_args()

    if args.model is None:
        args.model = DEFAULT_MODELS[args.api]

    # `bb run` chdir's into the Bazel runfiles tree, so resolving a relative
    # `--output-dir` against cwd would land artifacts in the runfiles cache.
    # `BUILD_WORKING_DIRECTORY` is the shell cwd before Bazel took over — use
    # that as the anchor for relative paths.
    cwd = Path(os.environ.get("BUILD_WORKING_DIRECTORY") or Path.cwd())
    raw = (
        Path(args.output_dir) if args.output_dir else Path("eval_results") / datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    )
    output_dir = raw if raw.is_absolute() else (cwd / raw).resolve()
    cases = [CASES[cid] for cid in (args.case or sorted(CASES))]

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    results = asyncio.run(
        _run_all(cases=cases, api=args.api, model=args.model, output_dir=output_dir, base_url=args.base_url)
    )

    print("\n=== Eval complete ===")
    for r in results:
        print(f"  {r.case_id}: {output_dir / r.case_id}")


if __name__ == "__main__":
    main()
