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
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse
from uuid import UUID

import httpx
from agent_framework import ChatContext, MiddlewareTermination, MiddlewareTypes, chat_middleware
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from devinfra.oci.oci import write_docker_auth
from props.agents.af.loop import make_agent, run_until_done
from props.agents.af.middleware import terminate_after_tools
from props.agents.critic_dev.loop import TEXT_OUTPUT_REMINDER, LoopState, LoopStatus, create_tools
from props.agents.runtime import get_current_agent_run, render_system_prompt, setup_logging
from props.core.agent_types import CriticDevImproveTypeConfig, CriticDevOptimizeTypeConfig
from props.core.ids import DefinitionId
from props.core.models.examples import SingleFileSetExample
from props.db.config import DatabaseConfig
from props.db.database import Database

# The critic-dev image bundles the recipe modules so in-container agents can import
# them (they are advertised in prompt_base.md.mako); no Python import reaches them.
# gazelle:include_dep //props/agents/critic_dev/recipes:examples_and_scopes
# gazelle:include_dep //props/agents/critic_dev/recipes:ground_truth
# gazelle:include_dep //props/agents/critic_dev/recipes:recall_metrics
# gazelle:include_dep //props/agents/critic_dev/recipes:run_analysis

logger = logging.getLogger(__name__)


def _setup_crane_auth(config: DatabaseConfig) -> None:
    """Write ~/.docker/config.json so crane can push/pull via Basic auth."""
    registry_url = os.environ.get("PROPS_REGISTRY_URL")
    if not registry_url:
        raise RuntimeError("PROPS_REGISTRY_URL must be set for crane auth setup")

    registry = urlparse(registry_url).netloc or registry_url
    write_docker_auth(registry, config.user, config.password, overwrite=True)
    logger.info("Crane auth configured for registry %s", registry)


class TerminationSuccess(BaseModel):
    definition_id: DefinitionId = Field(description="ID of the winning definition")
    total_credit: float = Field(description="Winning definition's sum of grader credits across allowed_examples")
    baseline_avg: float = Field(description="Average total_credit across baseline definitions")


def check_termination_condition(
    session: Session, improvement_run_id: UUID, type_config: CriticDevImproveTypeConfig
) -> TerminationSuccess | None:
    """Return the winner once a candidate covering every allowed example beats the baseline average, else `None`."""
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

    best_full = max(
        (row for row in candidate_results if (row.covered_examples or 0) >= n_examples),
        key=lambda row: row.total_credit or 0.0,
        default=None,
    )
    if best_full is None or baseline_avg is None:
        return None

    credit = best_full.total_credit or 0.0
    if credit <= baseline_avg:
        return None
    return TerminationSuccess(
        definition_id=DefinitionId(best_full.agent_definition_id), total_credit=credit, baseline_avg=baseline_avg
    )


def improve_termination_middleware(
    improvement_run_id: UUID,
    type_config: CriticDevImproveTypeConfig,
    db: Database,
    captured: dict[str, TerminationSuccess],
) -> Callable[[ChatContext, Callable[[], Awaitable[None]]], Awaitable[None]]:
    """Chat middleware (improve mode): before each model call, end the run once a candidate
    definition beats baseline. The captured success drives the exit code.
    """

    @chat_middleware
    async def middleware(_context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        with db.session() as session:
            result = check_termination_condition(
                session=session, improvement_run_id=improvement_run_id, type_config=type_config
            )
        if isinstance(result, TerminationSuccess):
            logger.info(
                "Critic developer terminating: definition '%s' with %.1f credit beats baseline avg %.1f",
                result.definition_id,
                result.total_credit,
                result.baseline_avg,
            )
            captured["result"] = result
            raise MiddlewareTermination("candidate beats baseline")
        await call_next()

    return middleware


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
    captured: dict[str, TerminationSuccess] = {}

    middleware: list[MiddlewareTypes] = [terminate_after_tools({"report_success", "report_failure"})]
    if isinstance(type_config, CriticDevImproveTypeConfig):
        middleware.append(improve_termination_middleware(agent_run_id, type_config, db, captured))

    agent = make_agent(
        db, instructions=system_prompt, tools=create_tools(state, http_client, db), middleware=middleware
    )

    try:
        await run_until_done(
            agent,
            done=lambda: state.status != LoopStatus.IN_PROGRESS or "result" in captured,
            reminder=TEXT_OUTPUT_REMINDER,
        )
    except Exception as exc:
        # Optimize runs until the proxy rejects further LLM calls for budget (HTTP 429);
        # that is the normal terminal condition, not a failure.
        if isinstance(type_config, CriticDevOptimizeTypeConfig) and "budget exceeded" in str(exc).lower():
            logger.info("Optimization completed (exhausted budget)")
            return 0
        raise

    if "result" in captured:
        result = captured["result"]
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
            "props/agents/critic_dev/prompt.md.mako", helpers={"type_config": type_config}
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
