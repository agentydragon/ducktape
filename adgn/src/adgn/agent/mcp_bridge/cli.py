"""CLI entry point for HTTP MCP Bridge.

Exposes RunningInfrastructure (Compositor + Policy Gateway) as an HTTP MCP server
that external agents can connect to.

Usage:
    adgn-mcp-bridge serve --agent-id external-chatgpt \\
        --db-path ./bridge.db \\
        --mcp-config ./docker-exec.json \\
        --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click
import docker
from fastmcp.mcp_config import MCPConfig
from platformdirs import user_data_dir
import uvicorn

from adgn.agent.mcp_bridge.server import (
    InfrastructureRegistry,
    create_bridge_infrastructure,
    create_mcp_server_app,
)
from adgn.agent.mcp_bridge.types import AgentID
from adgn.agent.persist.sqlite import SQLitePersistence

logger = logging.getLogger(__name__)

# Default database path in XDG user data directory
DEFAULT_DB_PATH = Path(user_data_dir("adgn", "agentydragon")) / "mcp-bridge.db"


@click.group()
def cli():
    """HTTP MCP Bridge - expose policy-gated infrastructure to external agents."""


@cli.command()
@click.option(
    "--agent-id",
    help="Agent identifier for single-agent mode (e.g., 'external-chatgpt'). Mutually exclusive with --auth-tokens.",
)
@click.option(
    "--auth-tokens",
    type=Path,
    help="Path to JSON token mapping file for multi-agent mode (token → agent_id). Mutually exclusive with --agent-id.",
)
@click.option(
    "--db-path", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})"
)
@click.option(
    "--mcp-config", type=Path, help="Path to .mcp.json config (servers to mount, e.g., docker exec with repo mount)"
)
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--mcp-port", type=int, default=8080, help="MCP server port (token-authenticated)")
@click.option("--initial-policy", type=Path, help="Path to initial approval policy (Python file)")
def serve(
    agent_id: str | None,
    auth_tokens: Path | None,
    db_path: Path,
    mcp_config: Path | None,
    host: str,
    mcp_port: int,
    initial_policy: Path | None,
):
    """Start HTTP MCP Bridge server.

    Two modes:
    - Single-agent: Use --agent-id (simple, one agent per bridge)
    - Multi-agent: Use --auth-tokens (token auth, multiple agents per bridge)
    """
    # Validate mutually exclusive options
    if agent_id and auth_tokens:
        raise click.UsageError("Cannot use both --agent-id and --auth-tokens")
    if not agent_id and not auth_tokens:
        raise click.UsageError("Must provide either --agent-id or --auth-tokens")
    if mcp_config:
        config = MCPConfig.model_validate_json(mcp_config.read_text())
    else:
        config = MCPConfig(mcpServers={})

    policy_source = initial_policy.read_text() if initial_policy else None

    asyncio.run(
        _run_server(
            agent_id=agent_id,
            auth_tokens_path=auth_tokens,
            db_path=db_path,
            mcp_config=config,
            host=host,
            mcp_port=mcp_port,
            initial_policy=policy_source,
        )
    )


async def _run_server(
    agent_id: str | None,
    auth_tokens_path: Path | None,
    db_path: Path,
    mcp_config: MCPConfig,
    host: str,
    mcp_port: int,
    initial_policy: str | None,
):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    persistence = SQLitePersistence(db_path)

    docker_client = docker.from_env()

    await persistence.ensure_schema()

    if agent_id:
        # Single-agent mode: create infrastructure at startup (no management UI)
        running = await create_bridge_infrastructure(
            agent_id=AgentID(agent_id),
            persistence=persistence,
            docker_client=docker_client,
            mcp_config=mcp_config,
            initial_policy=initial_policy,
        )

        async with running:
            mcp_app = running.compositor.http_app()

            logger.info("HTTP MCP Bridge started (single-agent mode)")
            logger.info(f"Agent ID: {agent_id}")
            logger.info(f"Compositor ready with {len(mcp_config.mcpServers or {})} external servers")
            logger.info(f"MCP server available at http://{host}:{mcp_port}/sse")

            mcp_config_obj = uvicorn.Config(app=mcp_app, host=host, port=mcp_port, log_level="info")
            mcp_server = uvicorn.Server(mcp_config_obj)
            await mcp_server.serve()
    else:
        assert auth_tokens_path is not None

        registry = InfrastructureRegistry(
            persistence=persistence, docker_client=docker_client, mcp_config=mcp_config, initial_policy=initial_policy
        )

        mcp_app = await create_mcp_server_app(auth_tokens_path=auth_tokens_path, registry=registry)

        logger.info("HTTP MCP Bridge started (multi-agent mode)")
        logger.info(f"Token mapping: {auth_tokens_path}")
        logger.info(f"MCP server (token auth): http://{host}:{mcp_port}/sse")

        mcp_config_obj = uvicorn.Config(app=mcp_app, host=host, port=mcp_port, log_level="info")
        mcp_server = uvicorn.Server(mcp_config_obj)

        await mcp_server.serve()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli()
