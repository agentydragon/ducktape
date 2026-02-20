"""Agent credential factories for integration tests.

Provides make_agent_credentials to create AgentRun records with corresponding
Postgres roles for RLS testing.
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel

from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus
from props.orchestration.agent_credentials import AgentCredentials, ensure_agent_role
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import ensure_fake_agent_definitions


async def make_agent_credentials(db: Database, type_config: BaseModel, image_digest: str) -> AgentCredentials:
    """Create an AgentRun record and Postgres role, returning credentials.

    Caller can extract agent_type from type_config directly (type_config.agent_type).
    """
    run_id = uuid4()
    with db.session() as session:
        ensure_fake_agent_definitions(session)
        agent_run = AgentRun(
            agent_run_id=run_id,
            image_digest=image_digest,
            model=DEFAULT_TEST_MODEL,
            status=AgentRunStatus.EXITED,
            type_config=type_config.model_dump(),
            budget_usd=5.0,
        )
        session.add(agent_run)
        session.commit()
    return await ensure_agent_role(db.config, run_id)
