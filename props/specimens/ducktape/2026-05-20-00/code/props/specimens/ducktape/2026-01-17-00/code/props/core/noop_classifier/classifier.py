"""No-op command classifier using MCP pattern.

Classifies docker/runtime exec command prefixes as either no-ops (useless commands
with no side effects) or potentially useful commands.

Uses work-stealing parallelism: multiple workers grab batches from a shared queue
as they finish, providing automatic load balancing.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import logging

from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from agent_core.agent import Agent
from agent_core.handler import AbortIf, BaseHandler
from agent_core.loop_control import RequireAnyTool
from mcp_infra.compositor.server import Compositor
from mcp_infra.display.rich_display import CompactDisplayHandler
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import OpenAIModelProto, SystemMessage, UserMessage
from props.core.noop_classifier.models import Classification, ClassifierState, SubmitClassificationsInput, SubmitResult

logger = logging.getLogger(__name__)

# MCP mount prefix for classifier server
CLASSIFIER_MOUNT_PREFIX = MCPMountPrefix("classifier")

# =============================================================================
# System Prompt
# =============================================================================

# Load from prompt.md at module import time (using importlib.resources for Bazel compatibility)
SYSTEM_PROMPT = importlib.resources.files("props.core.noop_classifier").joinpath("prompt.md").read_text()


# =============================================================================
# MCP Server Setup
# =============================================================================

CLASSIFIER_INSTRUCTIONS = """Submit classifications for command prefixes.

Call submit_classifications with a list of Classification objects."""


class ClassifierServer(EnhancedFastMCP):
    """Classifier MCP server with typed tool access.

    Provides submit_classifications tool for batch classification workflows.
    """

    # Tool reference (assigned in __init__)
    submit_classifications_tool: FunctionTool

    def __init__(self, state: ClassifierState):
        """Create classifier server with state container.

        Args:
            state: Classifier state with batch queue and results tracking
        """
        super().__init__("classifier", instructions=CLASSIFIER_INSTRUCTIONS)

        # Register tool - name derived from function name
        async def submit_classifications(payload: SubmitClassificationsInput) -> SubmitResult:
            """Submit classifications for current batch and get next batch."""
            if state.current_batch is None:
                raise ToolError("No batch active")

            expected = set(state.current_batch)
            submitted = {c.prefix for c in payload.classifications}

            # Validate
            missing = expected - submitted
            if missing:
                raise ToolError(f"Missing: {sorted(missing)}")

            extra = submitted - expected
            if extra:
                raise ToolError(f"Unexpected: {sorted(extra)}")

            # Store results
            state.results.extend(payload.classifications)

            # Get next batch for continuous processing
            next_batch = state.get_next_batch()

            # Report progress based on batches processed (total unknown in queue mode)
            if next_batch is None:
                progress = f"Complete: {state.batches_processed} batches processed"
            else:
                progress = f"Batch {state.batches_processed} processed, continuing..."

            return SubmitResult(ok=True, next_batch=next_batch, progress=progress)

        self.submit_classifications_tool = self.flat_model()(submit_classifications)


# =============================================================================
# Parallel Workers with Shared Pool (Work Stealing)
# =============================================================================


async def classify_worker(
    worker_id: int,
    batch_queue: asyncio.Queue[list[str]],
    results_queue: asyncio.Queue[list[Classification]],
    client: OpenAIModelProto,
    verbose: bool = False,
) -> None:
    """Worker that grabs batches from shared pool until empty.

    Each worker creates one agent that processes multiple batches:
    1. Creates compositor and classifier server
    2. Creates agent once
    3. Agent grabs first batch from queue
    4. Agent processes batch and tool returns next batch from queue
    5. Agent continues until queue is empty (tool returns None)
    6. Results are collected throughout

    Args:
        worker_id: Worker identifier for logging
        batch_queue: Shared queue of batches to process
        results_queue: Shared queue for results
        client: OpenAI client
        verbose: Enable verbose display output
    """
    logger.info(f"Worker {worker_id}: Starting")

    # Each worker gets its own compositor
    async with Compositor() as handle:
        # Create state that will grab batches from the shared queue
        state = ClassifierState(batch_queue)

        # Mount classifier server
        classifier_server = ClassifierServer(state)
        await handle.mount_inproc(CLASSIFIER_MOUNT_PREFIX, classifier_server)

        async with Client(handle) as mcp_client:
            # Grab first batch
            first_batch = state.get_next_batch()
            if first_batch is None:
                logger.info(f"Worker {worker_id}: No batches to process")
                return

            # Build handlers
            handlers: list[BaseHandler] = [AbortIf(should_abort=lambda: state.is_complete)]
            if verbose:
                display_handler = await CompactDisplayHandler.from_compositor(handle, prefix=f"[Worker {worker_id}] ")
                handlers.append(display_handler)

            # Create agent once for all batches
            agent = await Agent.create(
                mcp_client=mcp_client,
                client=client,
                handlers=handlers,
                dynamic_instructions=handle.render_agent_dynamic_instructions,
                tool_policy=RequireAnyTool(),
            )

            prompt = f"""Here are command prefixes to classify:

{json.dumps(first_batch, indent=2)}

Classify each prefix and submit using submit_classifications.
The tool will return the next batch for you to classify."""

            agent.process_message(SystemMessage.text(SYSTEM_PROMPT))
            agent.process_message(UserMessage.text(prompt))
            # Agent runs continuously: classify → submit → get next batch → repeat
            await agent.run()

        # Put all results in results queue
        await results_queue.put(state.results)

    logger.info(f"Worker {worker_id}: Finished, processed {state.batches_processed} batches")


async def classify_patterns_parallel(
    patterns: list[str], client: OpenAIModelProto, num_workers: int = 8, batch_size: int = 10, verbose: bool = False
) -> list[Classification]:
    """Classify patterns using parallel workers with shared work pool.

    Workers grab batches from a shared queue as they finish,
    providing automatic load balancing.

    Args:
        patterns: All command prefixes
        client: OpenAI client
        num_workers: Number of parallel workers
        batch_size: Size of each batch in the pool
        verbose: Enable verbose display output

    Returns:
        Combined classifications from all workers
    """
    # Create batches
    batches = [patterns[i : i + batch_size] for i in range(0, len(patterns), batch_size)]

    # Create shared queues
    batch_queue: asyncio.Queue[list[str]] = asyncio.Queue()
    results_queue: asyncio.Queue[list[Classification]] = asyncio.Queue()

    # Fill batch queue
    for batch in batches:
        await batch_queue.put(batch)

    logger.info(f"Starting {num_workers} workers to process {len(batches)} batches")

    # Start workers in parallel
    workers = [classify_worker(i, batch_queue, results_queue, client, verbose) for i in range(num_workers)]

    await asyncio.gather(*workers)

    # Collect all results
    all_results: list[Classification] = []
    while not results_queue.empty():
        batch_results = await results_queue.get()
        all_results.extend(batch_results)

    logger.info(f"Completed: {len(all_results)} classifications from {len(batches)} batches")

    return all_results
