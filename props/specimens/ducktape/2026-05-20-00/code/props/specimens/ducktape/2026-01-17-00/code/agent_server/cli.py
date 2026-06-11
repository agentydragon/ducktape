from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path
from urllib.parse import urlencode, urlunparse

import typer
import uvicorn
from typer.main import get_command

from agent_server.mcp_bridge.auth import TokensConfig
from agent_server.server.app import create_app
from cli_util.decorators import async_run
from cli_util.logging import make_logging_callback
from mcp_infra.config_loader import build_mcp_config
from net_util.net import pick_free_port

# Typer Option defaults must not be created in function signatures (ruff B008)
HOST_OPT = typer.Option("127.0.0.1", "--host", help="Host to bind UI server")
PORT_OPT = typer.Option(8765, "--port", help="Port to bind UI server")
FRONTEND_PORT_OPT = typer.Option(5173, "--frontend-port", help="Port for Vite dev server")
MCP_CONFIGS_OPT = typer.Option(
    [],
    "--mcp-config",
    help="Additional .mcp.json file(s) to merge (repeatable). Baseline: CWD/.mcp.json is always loaded if present.",
    exists=True,
    file_okay=True,
    dir_okay=False,
    readable=True,
    resolve_path=True,
)

app = typer.Typer(help="Agent Server CLI — serve the local UI server.", no_args_is_help=True)

# Configure logging via shared callback (default: INFO level)
app.callback()(make_logging_callback())


def _print_enabled(servers: list[str]) -> None:
    print("MCP servers enabled:", ", ".join(servers) if servers else "<none>")
    print("Tip: prefer HTTP specs; inproc factory specs are embedded over HTTP")


def _build_cfg_and_print(mcp_configs: list[Path]):
    cfg = build_mcp_config(mcp_configs)
    _print_enabled(list(cfg.mcpServers.keys()))
    return cfg


def _print_auth_url(host: str, port: int) -> None:
    """Print the authenticated URL for accessing the UI."""
    config = TokensConfig.from_yaml_file()
    if user_tokens := config.user_tokens():
        # Use first user token
        token = next(iter(user_tokens.keys()))
        query = urlencode({"token": token})
        url = urlunparse(("http", f"{host}:{port}", "", "", query, ""))
        print(f"\nAuthenticated URL: {url}")
    else:
        print("\nNo user tokens found. Create ~/.config/adgn/tokens.yaml with:")
        print("  users:")
        print('    admin: "your-hex-token"')


@app.command("serve")
@async_run
async def serve(host: str = HOST_OPT, port: int = PORT_OPT, mcp_configs: list[Path] = MCP_CONFIGS_OPT) -> None:
    """Launch the local FastAPI UI server and keep running.

    Tip: Use --log-level=DEBUG to show detailed OpenAI traffic.
    """
    print("Agent serve: starting agent + UI server")

    _ = _build_cfg_and_print(mcp_configs)
    # Build the FastAPI app; agent lifecycle is handled by the runtime container (registry)
    fastapi_app = create_app()

    config = uvicorn.Config(fastapi_app, host=host, port=port, log_level="debug")
    server = uvicorn.Server(config)
    print(f"\nUI server running at http://{host}:{port}")
    _print_auth_url(host, port)

    await server.serve()


@app.command("dev")
def dev(
    host: str = HOST_OPT,
    port: int = PORT_OPT,
    frontend_port: int = FRONTEND_PORT_OPT,
    mcp_configs: list[Path] = MCP_CONFIGS_OPT,
    open_browser: bool = typer.Option(True, "--open-browser/--no-open-browser"),
) -> None:
    """Run dev mode: Vite frontend (HMR) + backend in one command.

    Tip: Use --log-level=DEBUG to show detailed OpenAI traffic.
    """
    # UI lives in agent_server/web
    web_dir = Path(__file__).parent / "web"
    if not (web_dir / "package.json").exists():
        typer.echo(f"web UI directory not found: {web_dir}")
        raise typer.Exit(code=2)

    # Print merged config for visibility; the UI will attach via presets/API
    _ = _build_cfg_and_print(mcp_configs)

    # Pick free ports starting from requested bases
    backend_port = pick_free_port(host, preferred=port)
    frontend_dev_port = pick_free_port(host, preferred=frontend_port)
    if backend_port != port:
        typer.echo(f"Port {port} busy, using {backend_port} for backend")
    if frontend_dev_port != frontend_port:
        typer.echo(f"Port {frontend_port} busy, using {frontend_dev_port} for frontend")

    # Prepare Vite environment so frontend can reach backend on a different port
    vite_env = os.environ.copy()
    vite_env["VITE_BACKEND_ORIGIN"] = urlunparse(("http", f"{host}:{backend_port}", "", "", "", ""))

    # Start Vite dev server (HMR)
    vite_cmd = ["npm", "--prefix", str(web_dir), "run", "dev", "--", "--port", str(frontend_dev_port), "--strictPort"]
    typer.echo(f"Starting Vite dev server: {' '.join(vite_cmd)}")
    try:
        vite_proc = subprocess.Popen(vite_cmd, env=vite_env)
    except FileNotFoundError:
        typer.echo("npm not found. Please install Node/npm for frontend dev mode.")
        raise typer.Exit(code=2) from None

    try:
        url = urlunparse(("http", f"{host}:{frontend_dev_port}", "", "", "", ""))
        typer.echo(f"Frontend (HMR): {url}")
        backend_url = urlunparse(("http", f"{host}:{backend_port}", "", "", "", ""))
        typer.echo(f"Backend (MCP): {backend_url}")
        _print_auth_url(host, frontend_dev_port)
        if open_browser:
            # Try to open authenticated URL if token available
            config = TokensConfig.from_yaml_file()
            if user_tokens := config.user_tokens():
                token = next(iter(user_tokens.keys()))
                query = urlencode({"token": token})
                auth_url = urlunparse(("http", f"{host}:{frontend_dev_port}", "", "", query, ""))
            else:
                auth_url = url
            with contextlib.suppress(Exception):
                subprocess.Popen(["open", auth_url])

        # Build FastAPI app; agent lifecycle is handled by the runtime container (registry)
        app_fastapi = create_app()

        # Run uvicorn (restart this command when backend changes for now)
        uvicorn.run(app_fastapi, host=host, port=backend_port, log_level="debug")
    finally:
        with contextlib.suppress(Exception):
            vite_proc.terminate()
        with contextlib.suppress(Exception):
            vite_proc.wait(timeout=5)


main = get_command(app)

if __name__ == "__main__":
    main()
