"""Reconcile Grocy user permissions against a declarative policy file.

Read-only is the default in every grocy household instance (DEFAULT_PERMISSIONS=none,
see ../app-base), so every auto-created user is born with no permissions.
This reconciler converges
each user listed in the policy to exactly its declared permission set (creating
the user via reverse-proxy auth if absent); unlisted users are left untouched.

Two triggers, same convergence: the one-shot Job (runs at bootstrap; policy or
image changes recreate it via the Flux force annotation, and the kustomization's
healthcheck gates on its completion) and the daily CronJob (drift correction —
an operator demoted or haku elevated in the web UI converges back within a day).

Idempotent. Runs as the built-in `admin` user (always ADMIN, created by the
schema migration) via Grocy's trusted X-authentik-username header; the sibling
NetworkPolicy is what restricts who may present it.
"""

import contextlib
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import yaml
from pydantic import BaseModel, Field

ADMIN = "admin"  # Grocy's built-in admin user (created by the schema migration)


class Policy(BaseModel):
    users: dict[str, set[str]] = Field(
        description="username -> permission_hierarchy names the user holds exactly"
        " (empty = no permissions, i.e. read-only)"
    )


def wait_for_schema(client: httpx.Client) -> None:
    """Await Grocy's schema. The app self-migrates at startup (postStart hits the
    auth-exempt `/`) and Flux only runs the Job once grocy is Ready, but poll
    defensively so this also works run standalone (e.g. the CronJob racing a fresh
    deploy). Hit `/` (its body is an HTML redirect — ignore it) in case migration
    hasn't run, then poll the JSON `/api/users` until the schema exists.
    """
    for _ in range(30):
        # Transient connection errors and non-JSON bodies (mid-migration HTML)
        # are all just "not ready yet" — keep polling.
        with contextlib.suppress(httpx.HTTPError, ValueError):
            client.get("/")
            resp = client.get("/api/users")
            if resp.status_code == 200 and isinstance(resp.json(), list):
                return
        time.sleep(2)
    raise RuntimeError("Grocy schema not ready in time")


def get_json(client: httpx.Client, path: str) -> Any:
    resp = client.get(path)
    resp.raise_for_status()
    return resp.json()


def reconcile(client: httpx.Client, policy: Policy) -> None:
    # Grocy serializes SQLite rows with numeric columns as strings — int() them.
    permission_ids = {row["name"]: int(row["id"]) for row in get_json(client, "/api/objects/permission_hierarchy")}
    # Validate every name upfront so a typo can't leave the policy half-applied.
    if unknown := {name for names in policy.users.values() for name in names} - permission_ids.keys():
        raise ValueError(f"unknown permission names {sorted(unknown)}; valid: {sorted(permission_ids)}")
    for username in policy.users:
        # Reverse-proxy auth auto-creates the user on the first request bearing its
        # username; default-deny means it is born with no permissions.
        client.get("/api/system/info", headers={"X-authentik-username": username}).raise_for_status()
    user_ids = {u["username"]: int(u["id"]) for u in get_json(client, "/api/users")}
    for username, permission_names in policy.users.items():
        if username not in user_ids:
            raise RuntimeError(f"{username!r} user was not created by the reverse-proxy auth")
        user_id = user_ids[username]
        desired = {permission_ids[name] for name in permission_names}
        current = {int(row["permission_id"]) for row in get_json(client, f"/api/users/{user_id}/permissions")}
        if current == desired:
            print(f"{username!r} (id={user_id}) already at {sorted(permission_names)}")
            continue
        client.put(f"/api/users/{user_id}/permissions", json={"permissions": sorted(desired)}).raise_for_status()
        print(
            f"{username!r} (id={user_id}) converged to {sorted(permission_names)}"
            f" (permission ids {sorted(current)} -> {sorted(desired)})"
        )


def main(
    policy_path: Annotated[Path, typer.Option("--policy", exists=True, dir_okay=False, help="policy.yaml")],
    grocy_url: Annotated[str, typer.Option()],
) -> None:
    policy = Policy.model_validate(yaml.safe_load(policy_path.read_text()))
    with httpx.Client(
        base_url=grocy_url, headers={"X-authentik-username": ADMIN}, follow_redirects=True, timeout=30
    ) as client:
        wait_for_schema(client)
        reconcile(client, policy)


if __name__ == "__main__":
    typer.run(main)
