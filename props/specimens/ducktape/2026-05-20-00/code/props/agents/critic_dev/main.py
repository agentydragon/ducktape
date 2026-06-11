"""Critic developer agent main entry point for in-container execution.

Shared entry point for both optimize and improve modes. The type_config
discriminant determines behavior:
- CriticDevOptimizeTypeConfig: runs until budget exhaustion
- CriticDevImproveTypeConfig: auto-terminates when a candidate beats baseline
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from agent_core.agent import Agent
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.logging_handler import LoggingHandler
from agent_core.loop_control import Abort, AllowAnyToolOrTextMessage, InjectItems, LoopDecision, NoAction
from openai_utils.model import SystemMessage, UserMessage
from props.agents.critic_dev.loop import TEXT_OUTPUT_REMINDER, LoopState, LoopStatus, create_tool_provider
from props.agents.runtime import create_bound_model_from_env, get_current_agent_run, render_system_prompt, setup_logging
from props.core.agent_types import CriticDevImproveTypeConfig, CriticDevOptimizeTypeConfig
from props.core.ids import DefinitionId
from props.core.models.examples import SingleFileSetExample
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import GradingPending
from util.oci import write_docker_auth

logger = logging.getLogger(__name__)


# =============================================================================
# Crane auth
# =============================================================================


def _setup_crane_auth(config: DatabaseConfig) -> None:
    """Write ~/.docker/config.json so crane can push/pull via Basic auth."""
    backend_url = os.environ.get("PROPS_BACKEND_URL")
    if not backend_url:
        raise RuntimeError("PROPS_BACKEND_URL must be set for crane auth setup")

    registry = urlparse(backend_url).netloc
    write_docker_auth(registry, config.user, config.password, overwrite=True)
    logger.info("Crane auth configured for registry %s", registry)


# =============================================================================
# Improvement mode: termination checking
# =============================================================================


class TerminationSuccess(BaseModel):
    kind: Literal["success"] = "success"
    definition_id: DefinitionId = Field(description="ID of the winning definition")
    total_credit: float = Field(description="Winning definition's sum of grader credits across allowed_examples")
    baseline_avg: float = Field(description="Average total_credit across baseline definitions")


class BlockingStatus(BaseModel):
    kind: Literal["blocking"] = "blocking"
    message: str = Field(description="Human-readable explanation of what's blocking termination")
    baseline_avg_credit: float | None = Field(
        default=None, description="Average total_credit across baseline definitions"
    )
    best_candidate_credit: float | None = Field(default=None, description="Best candidate's total_credit")
    best_candidate_id: str | None = Field(default=None, description="ID of the best candidate definition so far")
    edges_needing_grading_count: int = Field(
        default=0, description="Number of edges in grading_pending still awaiting grading"
    )


TerminationResult = Annotated[TerminationSuccess | BlockingStatus, Field(discriminator="kind")]


def check_termination_condition(
    session: Session, improvement_run_id: UUID, type_config: CriticDevImproveTypeConfig
) -> TerminationResult:
    baseline_ids = type_config.baseline_image_digests
    allowed_examples = type_config.allowed_examples
    n_examples = len(allowed_examples)

    baseline_query = text("""
        WITH allowed_examples AS (
            SELECT
                unnest(:snapshot_slugs) AS snapshot_slug,
                unnest(:example_kinds) AS example_kind,
                unnest(:files_hashes) AS files_hash
        ),
        baseline_issues AS (
            SELECT
                oc.critic_image_digest AS agent_definition_id,
                SUM(oc.found_credit) as total_credit
            FROM tp_occurrence_credits oc
            JOIN allowed_examples ae ON (
                oc.snapshot_slug = ae.snapshot_slug
                AND oc.example_kind::text = ae.example_kind
                AND COALESCE(oc.files_hash, '') = COALESCE(ae.files_hash, '')
            )
            WHERE oc.critic_image_digest = ANY(:baseline_ids)
            GROUP BY oc.critic_image_digest
        )
        SELECT AVG(total_credit) as avg_credit
        FROM baseline_issues
    """).bindparams(
        bindparam("baseline_ids", type_=ARRAY(String)),
        bindparam("snapshot_slugs", type_=ARRAY(String)),
        bindparam("example_kinds", type_=ARRAY(String)),
        bindparam("files_hashes", type_=ARRAY(String)),
    )

    snapshot_slugs = [str(ex.snapshot_slug) for ex in allowed_examples]
    example_kinds = [ex.kind for ex in allowed_examples]
    files_hashes = [ex.files_hash if isinstance(ex, SingleFileSetExample) else "" for ex in allowed_examples]

    baseline_result = session.execute(
        baseline_query,
        {
            "baseline_ids": baseline_ids,
            "snapshot_slugs": snapshot_slugs,
            "example_kinds": example_kinds,
            "files_hashes": files_hashes,
        },
    ).fetchone()

    baseline_avg = baseline_result.avg_credit if baseline_result and baseline_result.avg_credit else None

    candidate_query = text("""
        WITH allowed_examples AS (
            SELECT
                unnest(:snapshot_slugs) AS snapshot_slug,
                unnest(:example_kinds) AS example_kind,
                unnest(:files_hashes) AS files_hash
        ),
        candidate_defs AS (
            SELECT digest as agent_definition_id
            FROM agent_definitions
            WHERE created_by_agent_run_id = :improvement_run_id
        ),
        candidate_coverage AS (
            SELECT
                cd.agent_definition_id,
                COUNT(DISTINCT (oc.snapshot_slug, oc.example_kind, COALESCE(oc.files_hash, ''))) as covered_examples,
                SUM(oc.found_credit) as total_credit
            FROM candidate_defs cd
            LEFT JOIN tp_occurrence_credits oc ON oc.critic_image_digest = cd.agent_definition_id
            LEFT JOIN allowed_examples ae ON (
                oc.snapshot_slug = ae.snapshot_slug
                AND oc.example_kind::text = ae.example_kind
                AND COALESCE(oc.files_hash, '') = COALESCE(ae.files_hash, '')
            )
            WHERE ae.snapshot_slug IS NOT NULL OR oc.snapshot_slug IS NULL
            GROUP BY cd.agent_definition_id
        )
        SELECT
            agent_definition_id,
            covered_examples,
            total_credit
        FROM candidate_coverage
        ORDER BY total_credit DESC NULLS LAST
    """).bindparams(
        bindparam("snapshot_slugs", type_=ARRAY(String)),
        bindparam("example_kinds", type_=ARRAY(String)),
        bindparam("files_hashes", type_=ARRAY(String)),
    )

    candidate_results = session.execute(
        candidate_query,
        {
            "improvement_run_id": str(improvement_run_id),
            "snapshot_slugs": snapshot_slugs,
            "example_kinds": example_kinds,
            "files_hashes": files_hashes,
        },
    ).fetchall()

    @dataclass
    class _CandidateScore:
        definition_id: str
        total_credit: float
        coverage: int

    best_full: _CandidateScore | None = None
    best_partial: _CandidateScore | None = None

    for row in candidate_results:
        covered = row.covered_examples or 0
        credit = row.total_credit or 0.0

        if covered >= n_examples:
            if best_full is None or credit > best_full.total_credit:
                best_full = _CandidateScore(row.agent_definition_id, credit, covered)
        elif (
            best_partial is None
            or covered > best_partial.coverage
            or (covered == best_partial.coverage and credit > best_partial.total_credit)
        ):
            best_partial = _CandidateScore(row.agent_definition_id, credit, covered)

    # Count edges awaiting grading from the grading_pending view
    pending_grading_edges = (
        session.query(func.count())
        .select_from(GradingPending)
        .filter(GradingPending.snapshot_slug.in_(snapshot_slugs))
        .scalar()
        or 0
    )

    if best_full is not None:
        if baseline_avg is None:
            return BlockingStatus(
                message=(
                    f"Definition '{best_full.definition_id}' has {best_full.total_credit:.1f} total credit, "
                    f"but baseline definitions have no evals yet. "
                    f"{pending_grading_edges} edges still awaiting grading."
                ),
                best_candidate_credit=best_full.total_credit,
                best_candidate_id=best_full.definition_id,
                edges_needing_grading_count=pending_grading_edges,
            )

        if best_full.total_credit > baseline_avg:
            return TerminationSuccess(
                definition_id=DefinitionId(best_full.definition_id),
                total_credit=best_full.total_credit,
                baseline_avg=baseline_avg,
            )

        return BlockingStatus(
            message=(
                f"Definition '{best_full.definition_id}' has {best_full.total_credit:.1f} total credit, "
                f"but baseline average is {baseline_avg:.1f}. "
                f"Need better credit or create a better definition."
            ),
            baseline_avg_credit=baseline_avg,
            best_candidate_credit=best_full.total_credit,
            best_candidate_id=best_full.definition_id,
        )

    if not candidate_results:
        return BlockingStatus(
            message=(
                "No definitions created yet. "
                "Create an improved definition at /workspace/improved/ and call create_definition."
            ),
            baseline_avg_credit=baseline_avg,
            edges_needing_grading_count=pending_grading_edges,
        )

    assert best_partial is not None
    missing_examples = n_examples - best_partial.coverage
    return BlockingStatus(
        message=(
            f"Definition '{best_partial.definition_id}' has evals for {best_partial.coverage}/{n_examples} examples. "
            f"Run evals for the remaining {missing_examples} examples to check if it beats baseline "
            f"(baseline avg: {baseline_avg:.1f} credit)."
            if baseline_avg
            else f"Definition '{best_partial.definition_id}' has evals for {best_partial.coverage}/{n_examples} examples. "
            f"Run evals for the remaining {missing_examples} examples. "
            f"{pending_grading_edges} edges still awaiting grading."
        ),
        baseline_avg_credit=baseline_avg,
        best_candidate_credit=best_partial.total_credit,
        best_candidate_id=best_partial.definition_id,
        edges_needing_grading_count=pending_grading_edges,
    )


class ImprovementReminderHandler(BaseHandler):
    def __init__(self, improvement_run_id: UUID, type_config: CriticDevImproveTypeConfig, db: Database):
        self._improvement_run_id = improvement_run_id
        self._type_config = type_config
        self._db = db
        self._text_detected = False
        self._last_result: TerminationResult | None = None

    def on_assistant_text_event(self, evt) -> None:
        self._text_detected = True

    def on_before_sample(self) -> LoopDecision:
        with self._db.session() as session:
            result = check_termination_condition(
                session=session, improvement_run_id=self._improvement_run_id, type_config=self._type_config
            )

        self._last_result = result

        if isinstance(result, TerminationSuccess):
            logger.info(
                f"Critic developer terminating: "
                f"definition '{result.definition_id}' with {result.total_credit:.1f} credit "
                f"beats baseline avg {result.baseline_avg:.1f}"
            )
            return Abort()

        if self._text_detected:
            self._text_detected = False
            return InjectItems(items=[UserMessage.text(self._build_reminder(result))])

        return NoAction()

    def _build_reminder(self, status: BlockingStatus) -> str:
        lines = ["=== Critic Developer Status ===", "", f"Blocking: {status.message}", ""]

        if status.baseline_avg_credit is not None:
            lines.append(f"Baseline average credit: {status.baseline_avg_credit:.1f}")

        if status.best_candidate_credit is not None:
            lines.append(f"Best candidate credit: {status.best_candidate_credit:.1f} ({status.best_candidate_id})")

        if status.edges_needing_grading_count > 0:
            lines.append(f"Edges awaiting grading: {status.edges_needing_grading_count}")

        lines.extend(
            [
                "",
                "Next steps:",
                "1. Build a custom critic image with crane (overlay main.py, push by digest)",
                "2. Run evals on your definition with run_critic (pass the digest as definition_id)",
                "3. Wait for grading with wait_until_graded_tool, then check recall views",
                "4. Iterate: refine definition, re-eval, until you beat baseline",
                "",
                "Do NOT send text messages - execute your plan with tools.",
            ]
        )

        return "\n".join(lines)

    @property
    def last_result(self) -> TerminationResult | None:
        return self._last_result


# =============================================================================
# Agent loop
# =============================================================================


async def run_agent_loop(
    system_prompt: str,
    http_client: httpx.AsyncClient,
    db: Database,
    agent_run_id: UUID,
    type_config: CriticDevOptimizeTypeConfig | CriticDevImproveTypeConfig,
) -> int:
    """Run the critic developer agent loop.

    For optimize: runs until budget/timeout exhaustion.
    For improve: auto-terminates when a candidate beats baseline.
    """
    state = LoopState()
    tool_provider = create_tool_provider(state, http_client, db)
    bound_model = create_bound_model_from_env(db)

    handlers: list[BaseHandler] = [
        LoggingHandler(logger),
        RedirectOnTextMessageHandler(TEXT_OUTPUT_REMINDER),
        AbortIf(lambda: state.status != LoopStatus.IN_PROGRESS),
    ]

    reminder_handler: ImprovementReminderHandler | None = None
    if isinstance(type_config, CriticDevImproveTypeConfig):
        reminder_handler = ImprovementReminderHandler(improvement_run_id=agent_run_id, type_config=type_config, db=db)
        handlers.append(reminder_handler)

    agent = await Agent.create(
        tool_provider=tool_provider,
        handlers=handlers,
        client=bound_model,
        parallel_tool_calls=True,
        tool_policy=AllowAnyToolOrTextMessage(),
    )

    agent.process_message(SystemMessage.text(system_prompt))
    await agent.run()

    if reminder_handler is not None and isinstance(reminder_handler.last_result, TerminationSuccess):
        result = reminder_handler.last_result
        logger.info(
            "Critic developer succeeded: definition '%s' with %.1f credit beats baseline avg %.1f",
            result.definition_id,
            result.total_credit,
            result.baseline_avg,
        )
        return 0

    match state.status:
        case LoopStatus.EXITED_SUCCESS:
            logger.info("Critic developer reported success")
            return 0
        case LoopStatus.EXITED_FAILURE:
            logger.info("Critic developer failed")
            return 1
        case LoopStatus.IN_PROGRESS:
            if isinstance(type_config, CriticDevOptimizeTypeConfig):
                logger.info("Optimization completed (exhausted budget)")
                return 0
            logger.warning("Agent finished without beating baseline")
            return 1


# =============================================================================
# Entry point
# =============================================================================


async def main() -> int:
    """Main entry point for critic developer agent."""
    setup_logging()

    db = Database.from_env()
    _setup_crane_auth(db.config)

    with db.session() as session:
        agent_run = get_current_agent_run(session)
        agent_run_id = agent_run.agent_run_id
        type_config = agent_run.type_config
        logger.info("Critic developer starting: %s, run=%s", type_config.agent_type.value, agent_run_id)

    if not isinstance(type_config, (CriticDevOptimizeTypeConfig, CriticDevImproveTypeConfig)):
        logger.error("Unexpected type_config: %s", type(type_config).__name__)
        return 1

    backend_url = os.environ.get("PROPS_BACKEND_URL", "http://props-backend:8000")
    async with httpx.AsyncClient(
        base_url=backend_url, auth=(db.config.user, db.config.password), timeout=httpx.Timeout(60.0, connect=30.0)
    ) as http_client:
        system_prompt = render_system_prompt(
            "props/agents/critic_dev/prompt.md.mako", db, helpers={"type_config": type_config}
        )

        exit_code = await run_agent_loop(
            system_prompt=system_prompt,
            http_client=http_client,
            db=db,
            agent_run_id=agent_run_id,
            type_config=type_config,
        )

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
