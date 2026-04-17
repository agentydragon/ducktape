"""CLI for running the Grocy MCP eval against an existing Grocy instance."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from x.grocy_mcp.eval.run import DEFAULT_MODELS, run_grocy_eval


def main() -> None:
    parser = argparse.ArgumentParser(description="Grocy MCP eval — run an agent against Grocy and record the rollout")
    parser.add_argument("--api", choices=["openai", "anthropic"], default="openai", help="LLM API provider")
    parser.add_argument("--model", default=None, help="Model name (default depends on --api)")
    parser.add_argument("--grocy-url", required=True, help="URL of a running Grocy instance (e.g. http://localhost:9283)")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: eval_results/<timestamp>)")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL (for Ollama/LiteLLM)")
    args = parser.parse_args()

    if args.model is None:
        args.model = DEFAULT_MODELS[args.api]

    output_dir = Path(args.output_dir) if args.output_dir else Path("eval_results") / datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = asyncio.run(
        run_grocy_eval(
            api=args.api,
            model=args.model,
            grocy_base_url=args.grocy_url,
            output_dir=output_dir,
            base_url=args.base_url,
        )
    )

    print(f"\nEval complete:")
    print(f"  Model: {result.model} ({result.api})")
    print(f"  Task tool calls: {result.task_turns}")
    print(f"  Transcript: {result.transcript_path}")
    print(f"\nPostmortem:\n{result.postmortem_text}")


if __name__ == "__main__":
    main()
