#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from pathlib import Path

from sysrw.extract_common import write_jsonl_batches
from sysrw.extract_dataset_ccr import process_file as process_ccr_file

TRACE_DIR = Path.home() / ".claude-code-router" / "logs"
OUTPUT_PATH = Path(__file__).parent / "data" / "dataset.jsonl"


def list_trace_files() -> list[Path]:
    if not TRACE_DIR.exists():
        return []
    return [p for p in sorted(TRACE_DIR.glob("trace.*")) if p.is_file()]


async def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    files = list_trace_files()
    sem = asyncio.Semaphore(16)

    async def wrapped(p: Path):
        async with sem:
            return await process_ccr_file(p)

    results: list[list[dict]] = await asyncio.gather(*[wrapped(p) for p in files])
    write_jsonl_batches(results, OUTPUT_PATH, event="dataset_written")


if __name__ == "__main__":
    asyncio.run(main())
