"""CLI entrypoint for running rspcache proxy/admin services and managing API keys."""

from __future__ import annotations

from uuid import UUID

import httpx
import typer
import uvicorn

from rspcache.admin_app import APIKeyListModel, CreateKeyResponse

DEFAULT_PORTS = {"proxy": 8000, "admin": 8100}

DEFAULT_ADMIN_URL = "http://127.0.0.1:8100"

app = typer.Typer(help="rspcache servers")
keys_app = typer.Typer(help="Provision client API keys")
app.add_typer(keys_app, name="keys")


@app.command()
def run(
    app_name: str = typer.Option("proxy", "--app", "-a", help="Which app to run: proxy or admin"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int | None = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload"),
) -> None:
    """Run the rspcache proxy or admin application."""
    app_key = app_name.lower()
    if app_key not in DEFAULT_PORTS:
        raise typer.BadParameter("app must be 'proxy' or 'admin'")
    target = "rspcache:APP" if app_key == "proxy" else "rspcache.admin_app:ADMIN_APP"
    resolved_port = port or DEFAULT_PORTS[app_key]
    uvicorn.run(target, host=host, port=resolved_port, reload=reload)


def _admin_client(admin_url: str, token: str | None) -> httpx.Client:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=admin_url, headers=headers, timeout=30.0)


def _format_key_info(
    item_id: str | UUID, name: str, upstream_alias: str, token_prefix: str, created_at: str, status: str | None = None
) -> str:
    """Format API key info for display."""
    parts = [f"{item_id}", f"{name}", f"alias={upstream_alias}", f"prefix={token_prefix}"]
    if status:
        parts.append(f"status={status}")
    parts.append(f"created={created_at}")
    return "  ".join(parts)


@keys_app.command("mint")
def mint_key(
    name: str = typer.Argument(..., help="Display name for the client API key"),
    alias: str = typer.Option("default", "--alias", help="Upstream OpenAI key alias to use"),
    admin_url: str = typer.Option(DEFAULT_ADMIN_URL, "--admin-url", help="rspcache admin base URL"),
    bearer_token: str = typer.Option("", "--bearer-token", help="Bearer token for admin auth (if required)"),
) -> None:
    """Mint a new client API key via the admin API."""
    payload = {"name": name, "alias": alias}
    with _admin_client(admin_url, bearer_token or None) as client:
        resp = client.post("/api/keys", json=payload)
        if resp.status_code == 409:
            typer.secho("Key name already exists.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        if resp.status_code != 200:
            typer.secho(f"Failed to mint key: {resp.status_code} {resp.text}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        data = CreateKeyResponse.model_validate(resp.json())
    typer.secho("API key created successfully.", fg=typer.colors.GREEN)
    typer.secho("Token (copy now, it will not be shown again):", fg=typer.colors.YELLOW)
    typer.echo(data.token)
    record = data.record
    key_info = _format_key_info(
        record.id, record.name, record.upstream_alias, record.token_prefix, record.created_at.isoformat()
    )
    typer.echo(f"Name: {key_info}")


def _resolve_key_id(client: httpx.Client, name: str | None, key_id: str | None) -> UUID:
    if key_id:
        return UUID(key_id)
    if not name:
        typer.secho("Provide either --id or --name to identify the key.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    resp = client.get("/api/keys")
    if resp.status_code != 200:
        typer.secho(f"Failed to list keys: {resp.status_code} {resp.text}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    payload: APIKeyListModel = APIKeyListModel.model_validate(resp.json())
    for item in payload.items:
        if item.name == name:
            return UUID(str(item.id))
    typer.secho(f"No key found with name '{name}'.", fg=typer.colors.RED)
    raise typer.Exit(code=1)


@keys_app.command("revoke")
def revoke_key(
    key_id: str = typer.Option("", "--id", help="Key UUID to revoke"),
    name: str = typer.Option("", "--name", help="Key name to revoke"),
    admin_url: str = typer.Option(DEFAULT_ADMIN_URL, "--admin-url", help="rspcache admin base URL"),
    bearer_token: str = typer.Option("", "--bearer-token", help="Bearer token for admin auth (if required)"),
) -> None:
    """Revoke an existing client API key."""
    with _admin_client(admin_url, bearer_token or None) as client:
        resolved_id = _resolve_key_id(client, name or None, key_id or None)
        resp = client.post(f"/api/keys/{resolved_id}/revoke")
        if resp.status_code != 200:
            typer.secho(f"Failed to revoke key: {resp.status_code} {resp.text}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    typer.secho(f"Key {resolved_id} revoked.", fg=typer.colors.GREEN)


@keys_app.command("list")
def list_keys(
    admin_url: str = typer.Option(DEFAULT_ADMIN_URL, "--admin-url", help="rspcache admin base URL"),
    bearer_token: str = typer.Option("", "--bearer-token", help="Bearer token for admin auth (if required)"),
) -> None:
    """List client API keys."""
    with _admin_client(admin_url, bearer_token or None) as client:
        resp = client.get("/api/keys")
        if resp.status_code != 200:
            typer.secho(f"Failed to list keys: {resp.status_code} {resp.text}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        payload: APIKeyListModel = APIKeyListModel.model_validate(resp.json())
    items = payload.items
    if not items:
        typer.echo("No keys found.")
        return
    for item in items:
        status = "revoked" if item.revoked_at else "active"
        key_info = _format_key_info(
            item.id, item.name, item.upstream_alias, item.token_prefix, item.created_at.isoformat(), status=status
        )
        typer.echo(key_info)


if __name__ == "__main__":
    app()
