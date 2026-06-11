# No-Op Command Classifier Design

## Goal

Build a classifier to identify whether docker/runtime exec commands are definitively no-ops (useless commands that have no side effects).

## Architecture

### High-Level Flow

1. **Input**: JSON file with command patterns (`docker_exec_patterns.json`)
2. **Batching**: Split patterns into batches of 10
3. **Central Pool**: Queue of unprocessed batches
4. **Worker Agents**: N parallel agents (e.g., 4-8 agents)
5. **Output**: JSON file with classifications

### Work-Stealing Parallelism

Workers grab batches from shared `asyncio.Queue` as they finish:

1. Create `batch_queue` (all batches) and `results_queue` (empty)
2. Start N workers in parallel with `asyncio.gather()`
3. Each worker:
   - Creates own Compositor and classifier server
   - Grabs batch from queue with `batch_queue.get_nowait()`
   - Processes single batch (one `agent.run()` call)
   - Puts results in `results_queue`
   - Repeats until `asyncio.QueueEmpty`
4. Collect all results from `results_queue`

**Key insight:** No pre-splitting - fast workers process more batches.

### Worker Agent Pattern

Each agent is a stateful conversation with its own MCP server instance:

```
Agent Transcript:
- System: You are a classifier...
- User: Here are 10 command prefixes to classify: [batch 1]
- Assistant: tool_call(submit_classifications([...]))
- Tool: {"accepted": true, "next_batch": [...], "progress": "1/100"}
- Assistant: [classifies next batch]
- Assistant: tool_call(submit_classifications([...]))
- Tool: {"accepted": true, "next_batch": [...], "progress": "2/100"}
...
- Tool: {"accepted": true, "next_batch": null, "progress": "100/100 complete"}
- Assistant: Done!
```

**Context window handling:**

- If `context_length_exceeded` error: create new agent with new MCP server
- New server starts from where previous one left off (remaining batches)
- Original server's results are preserved

**Per-worker MCP server:**

- Each worker gets its own `ClassifierServer` instance
- Server tracks its own batches and results
- No shared state between workers

### MCP Server Pattern (Structured Output with Flow Control)

The tool both validates classifications AND returns the next batch:

```python
class ClassifierServer:
    def __init__(self, batches: list[list[str]]):
        self._batches = batches
        self._current_batch_idx = 0
        self._results: list[Classification] = []

    @mcp.tool()
    def submit_classifications(
        classifications: list[Classification]
    ) -> SubmitResult:
        # Validate all prefixes from current batch are classified
        # Raise ToolError if validation fails
        # Store results
        # Return next batch (or None if done)
```

**Schema:**

```python
class Classification(BaseModel):
    prefix: str
    is_noop: bool
    reason: str  # Brief explanation

class SubmitResult(BaseModel):
    accepted: Literal[True] = True
    next_batch: list[str] | None  # None = all batches done
    progress: str  # e.g., "Batch 5/752 complete"
```

**Validation:**

- Tool validates all prefixes from current batch are classified
- Raises `ToolError` if any prefix is missing
- Raises `ToolError` if any prefix appears twice
- Stores valid classifications
- Returns next batch to classify (or `None` when done)

**Flow:**

1. Initial user message contains first batch
2. Agent calls `submit_classifications([...])`
3. Tool validates, stores results, returns next batch
4. Agent classifies next batch, calls tool again
5. Repeat until tool returns `next_batch: None`

## System Prompt

```
You are a command classifier. Your task is to identify whether docker/runtime
exec commands are definitively no-ops (useless commands with no observable effects).

A command is a no-op if it has NO observable side effects or useful output:
- Displays information without capturing it (pwd, ls without redirection)
- Commands that do nothing by design (true, :, empty commands)
- Navigation without subsequent commands (cd alone, no && or ;)
- Echo/printf without file redirection (display only)
- Database connectivity tests without side effects (SELECT 1, SELECT 'READY')
- Python/shell print statements without redirection (print('hello'), echo test)
- Commands that only produce output for display, not for capture/processing
- Any command that produces no state changes and whose output is unused

A command is NOT a no-op if:
- It reads or writes files (sed with output, cat > file, etc.)
- It modifies state (git commands, database queries, file operations)
- It performs analysis/inspection with captured output (ruff, mypy, grep, etc.)
- Its output is captured for use (command substitution, pipes, redirection)
- It has network effects (curl, ssh, psql with side effects)
- It computes something that affects subsequent operations
- It's part of a command chain (&&, ||, |, ;)

Key principle: A no-op produces no lasting changes and its output (if any) is
discarded or only shown. When in doubt, classify as NOT a no-op.

You will exclusively act by calling tools. When you successfully submit classifications,
another batch will be sent to you. Continue until all batches are complete.
```

## Implementation

### Files to Create/Modify

1. **`props/cli/cmd_classify_noops.py`**
   - Main CLI command
   - Batch processing logic
   - Worker pool management
   - Error handling

2. **`props/core/noop_classifier/`**
   - Pydantic models (Classification, SubmitResult)
   - ClassifierServer (MCP server)
   - Classifier agent implementation
   - System prompt
   - Agent state management
   - Validation logic

### Integration Points

- Use existing OpenAI wrappers (`openai_utils.client_factory`)
- Use existing MCP patterns (`mcp_infra.*`)
- Output to JSON for further analysis

### Implementation Sketch

```python
from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from adgn.agent.agent import Agent
from mcp_infra.compositor.server import Compositor
from mcp_infra.enhanced import EnhancedFastMCP
from openai_utils.model import OpenAIModelProto

logger = logging.getLogger(__name__)


# =============================================================================
# Models
# =============================================================================

class Classification(BaseModel):
    prefix: str
    is_noop: bool
    reason: str


class SubmitResult(BaseModel):
    """Result from submitting classifications."""
    ok: Literal[True] = True
    next_batch: list[str] | None  # Next batch to classify, or None if complete
    progress: str


class ClassifierState:
    """Container for classifier state and results."""
    def __init__(self, patterns: list[str], batch_size: int = 10):
        self.batches = [
            patterns[i:i + batch_size]
            for i in range(0, len(patterns), batch_size)
        ]
        self.current_idx = 0
        self.results: list[Classification] = []
        self.current_batch: list[str] | None = None

    def get_next_batch(self) -> list[str] | None:
        """Get next batch and advance index."""
        if self.current_idx >= len(self.batches):
            return None
        batch = self.batches[self.current_idx]
        self.current_idx += 1
        self.current_batch = batch
        return batch

    @property
    def is_complete(self) -> bool:
        """Check if all batches processed."""
        return self.current_idx >= len(self.batches)


# =============================================================================
# MCP Server
# =============================================================================

def build_classifier_tools(mcp: EnhancedFastMCP, state: ClassifierState) -> None:
    """Register classifier tool."""

    @mcp.flat_model()
    async def submit_classifications(classifications: list[Classification]) -> SubmitResult:
        """Submit classifications for current batch and get next batch."""
        if state.current_batch is None:
            raise ToolError("No batch active")

        expected = set(state.current_batch)
        submitted = {c.prefix for c in classifications}

        missing = expected - submitted
        if missing:
            raise ToolError(f"Missing: {sorted(missing)}")

        extra = submitted - expected
        if extra:
            raise ToolError(f"Unexpected: {sorted(extra)}")

        state.results.extend(classifications)
        next_batch = state.get_next_batch()

        progress = f"Batch {state.current_idx - 1}/{len(state.batches)}"
        if next_batch is None:
            progress = f"Complete: {len(state.batches)}/{len(state.batches)} batches"

        return SubmitResult(ok=True, next_batch=next_batch, progress=progress)


CLASSIFIER_INSTRUCTIONS = """Submit classifications for command prefixes.

Call submit_classifications with a list of Classification objects."""


def make_classifier_server(state: ClassifierState) -> EnhancedFastMCP:
    """Create MCP server with classifier tools."""
    mcp = EnhancedFastMCP("classifier", instructions=CLASSIFIER_INSTRUCTIONS)
    build_classifier_tools(mcp, state)
    return mcp


# =============================================================================
# Agent Execution
# =============================================================================

async def classify_worker(
    worker_id: int,
    batch_queue: asyncio.Queue[list[str]],
    results_queue: asyncio.Queue[list[Classification]],
    client: OpenAIModelProto,
) -> None:
    """Worker that grabs batches from shared pool until empty."""
    logger.info(f"Worker {worker_id}: Starting")

    async with Compositor(f"compositor-worker-{worker_id}") as handle:
        async with Client(handle) as mcp_client:
            processed = 0

            while True:
                try:
                    batch = batch_queue.get_nowait()
                except asyncio.QueueEmpty:
                    logger.info(f"Worker {worker_id}: Queue empty, processed {processed} batches")
                    break

                logger.info(f"Worker {worker_id}: Processing batch {processed + 1}")

                state = ClassifierState(batch, batch_size=len(batch))
                state.get_next_batch()

                classifier_server = make_classifier_server(state)
                await handle.mount_inproc(f"classifier-{worker_id}", classifier_server)

                agent = await Agent.create(
                    mcp_client=mcp_client,
                    system=SYSTEM_PROMPT,
                    client=client,
                    handlers=[],
                )

                prompt = f"""Here are command prefixes to classify:

{json.dumps(batch, indent=2)}

Classify each prefix and submit using submit_classifications."""

                await agent.run(prompt)

                await results_queue.put(state.results)
                processed += 1

                await handle.detach(f"classifier-{worker_id}")

    logger.info(f"Worker {worker_id}: Finished, processed {processed} batches")


async def classify_patterns_parallel(
    patterns: list[str],
    client: OpenAIModelProto,
    num_workers: int = 8,
    batch_size: int = 10,
) -> list[Classification]:
    """Classify patterns using parallel workers with shared work pool."""
    batches = [
        patterns[i:i + batch_size]
        for i in range(0, len(patterns), batch_size)
    ]

    batch_queue: asyncio.Queue[list[str]] = asyncio.Queue()
    results_queue: asyncio.Queue[list[Classification]] = asyncio.Queue()

    for batch in batches:
        await batch_queue.put(batch)

    logger.info(f"Starting {num_workers} workers to process {len(batches)} batches")

    workers = [
        classify_worker(i, batch_queue, results_queue, client)
        for i in range(num_workers)
    ]

    await asyncio.gather(*workers)

    all_results: list[Classification] = []
    while not results_queue.empty():
        batch_results = await results_queue.get()
        all_results.extend(batch_results)

    logger.info(f"Completed: {len(all_results)} classifications from {len(batches)} batches")

    return all_results
```

## Output Format

```json
[
  {
    "prefix": "[\"pwd\"]",
    "is_noop": true,
    "reason": "pwd without output redirection (display only)"
  },
  {
    "prefix": "[\"ruff\", \"check\", ...]",
    "is_noop": false,
    "reason": "Runs static analysis tool with side effects"
  }
]
```

## Error Handling

1. **Context window exceeded**: Restart agent from empty transcript
2. **Missing classifications**: Retry batch with same agent
3. **API errors**: Exponential backoff, retry
4. **Validation errors**: Log and skip batch (manual review)

## Model Selection

Use `gpt-5.1-codex-mini` for:

- Optimized for code understanding
- Fast classification
- Cost efficiency
- Good at pattern recognition in command strings

## Performance Estimates

- 7,516 patterns
- Batches of 10 = ~752 batches
- 8 workers in parallel
- ~94 batches per worker
- Estimated time: 5-10 minutes
