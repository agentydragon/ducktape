"""Shared resources for CLI commands (dependency injection providers).

This module provides dependency functions for expensive resources that should be
created once per CLI invocation and reused across commands.

typer-di automatically caches these dependencies - each function is called once
per CLI invocation and the result is reused.

Usage in commands:
    from typer_di import Depends
    from .resources import get_database_config

    @app.command()
    def my_command(
        snapshot: str,
        db_config: DatabaseConfig = Depends(get_database_config),
    ):
        # db_config is injected automatically
        ...
"""

from __future__ import annotations

from ..db.config import DatabaseConfig, get_database_config as _get_database_config


def get_database_config() -> DatabaseConfig:
    """Get database configuration from environment variables.

    Reads PostgreSQL connection parameters from environment (set by devenv or passed to containers).

    typer-di calls this function only once per CLI invocation.
    """
    return _get_database_config()


# NOTE: No get_docker_client() dependency function.
#
# CLI commands create aiodocker.Docker() clients locally in async context.
# typer-di doesn't support async dependencies - they must be created
# inside the async command function.
#
# Example:
#
#   async def my_command(...):
#       async with aiodocker.Docker() as docker_client:
#           # use docker_client
#           ...
#
# This ensures the Docker client is created inside the running event loop.
