"""Store steps the notification tests repeat: claiming a due inbox's source and driving its migrations."""

from alembic.config import Config
from sqlalchemy.engine import Connection

from agentplane.notification_service.database_migrate import RUNNER
from agentplane.notification_service.db import Inbox, Subscription
from agentplane.notification_service.store import Store


async def claimed(store: Store) -> tuple[Inbox, Subscription]:
    """Claim the next due inbox and its due source; both must exist."""
    claim = await store.claim()
    assert claim is not None
    return claim, await due_source(store, claim)


async def due_source(store: Store, claim: Inbox) -> Subscription:
    """The claimed inbox's next due source, which must exist."""
    source = await store.source(claim)
    assert source is not None
    return source


def migration_config(connection: Connection) -> Config:
    """Alembic configuration over the image-owned migration chain, bound to an open connection."""
    config = Config()
    config.set_main_option("script_location", str(RUNNER.migrations_dir))
    config.attributes["connection"] = connection
    return config
