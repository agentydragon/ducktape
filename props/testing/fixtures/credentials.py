"""Agent credential factories for integration tests.

Provides make_agent_credentials to create AgentRun records with corresponding
Postgres roles for RLS testing.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from pydantic import BaseModel

from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, AgentType
from props.orchestration.agent_credentials import ensure_agent_role
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import ensure_fake_agent_definitions


@dataclass(frozen=True)
class CredentialsWithType:
    """Agent credentials with agent type from type_config."""

    username: str
    password: str
    agent_type: AgentType


async def make_agent_credentials(db: Database, type_config: BaseModel, image_digest: str) -> CredentialsWithType:
    """Create an AgentRun record and Postgres role, returning credentials with type."""
    run_id = uuid4()
    agent_type = None
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
        # Extract agent_type before session closes (while object is still attached)
        agent_type = agent_run.type_config.agent_type
    creds = await ensure_agent_role(db.config, run_id)
    assert agent_type is not None
    return CredentialsWithType(username=creds.username, password=creds.password, agent_type=agent_type)
