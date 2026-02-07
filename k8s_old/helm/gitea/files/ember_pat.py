#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests


def _env(key: str, default: str | None = None) -> str:
    value = os.environ.get(key, default)
    if value is None or value == "":
        raise RuntimeError(f"Environment variable {key} must be set")
    return value


@dataclass
class Settings:
    base_url: str
    username: str
    password: str
    email: str
    namespace: str
    secret_name: str
    token_name: str
    token_scopes: list[str]


def load_settings() -> Settings:
    base_url = _env("GITEA_BASE_URL").rstrip("/")
    username = _env("EMBER_USERNAME")
    password = _env("EMBER_PASSWORD")
    namespace = _env("EMBER_NAMESPACE")
    secret_name = _env("EMBER_SECRET_NAME")
    token_name = _env("EMBER_TOKEN_NAME", "ember-agent-token")
    email = os.environ.get("EMBER_EMAIL", f"{username}@local")
    scopes_raw = os.environ.get(
        "EMBER_TOKEN_SCOPES",
        "read:user,write:user,read:repository,write:repository,read:organization,write:organization,"
        "read:package,write:package,read:issue,write:issue,read:notification,write:notification",
    )
    scopes = [scope.strip() for scope in scopes_raw.split(",") if scope.strip()]
    return Settings(
        base_url=base_url,
        username=username,
        password=password,
        email=email,
        namespace=namespace,
        secret_name=secret_name,
        token_name=token_name,
        token_scopes=scopes,
    )


def run_git(command: str, *, capture_output: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = "/data/git"
    return subprocess.run(["su", "git", "-c", command], env=env, capture_output=capture_output, text=True, check=check)


def wait_for_gitea(base_url: str, timeout_seconds: int = 300) -> None:
    url = f"{base_url}/api/v1/version"
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code in (200, 401, 403):
                print(f"Gitea API reachable (status {response.status_code})", flush=True)
                return
            print(f"Gitea not ready yet (status {response.status_code}); retrying", flush=True)
        except requests.RequestException as exc:
            print(f"Error contacting Gitea API: {exc}; retrying", flush=True)
        time.sleep(5)
    raise RuntimeError("Gitea did not become ready in time")


def ensure_user(settings: Settings) -> None:
    result = run_git("HOME=/data/git /app/gitea/gitea admin user list", capture_output=True)
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == settings.username:
            print(f"Gitea user '{settings.username}' already exists", flush=True)
            return

    print(f"Creating Gitea user '{settings.username}'", flush=True)
    run_git(
        "HOME=/data/git /app/gitea/gitea admin user create "
        f"--username {settings.username} "
        f"--password {settings.password} "
        f"--email {settings.email} "
        "--must-change-password=false"
    )


def create_token(settings: Settings) -> tuple[str, str]:
    token_name = f"{settings.token_name}-{int(time.time())}"
    print(f"Generating access token '{token_name}'", flush=True)
    scopes_arg = ""
    if settings.token_scopes:
        scopes_arg = f"--scopes {','.join(settings.token_scopes)} "
    result = run_git(
        (
            "HOME=/data/git /app/gitea/gitea admin user generate-access-token "
            f"--username {settings.username} "
            f"--token-name {token_name} "
            f"{scopes_arg}"
            "--raw"
        ),
        capture_output=True,
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    token = lines[-1] if lines else ""
    if result.returncode != 0 or not token:
        message = result.stderr.strip() if result.stderr else result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Failed to generate access token: {message}")
    print("Minted Gitea personal access token", flush=True)
    return token_name, token


def upsert_secret(settings: Settings, token_name: str, token: str) -> None:
    host = _env("KUBERNETES_SERVICE_HOST")
    port = _env("KUBERNETES_SERVICE_PORT", "443")
    sa_token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
    ca_cert = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    api_server = f"https://{host}:{port}"

    payload = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": settings.secret_name,
            "namespace": settings.namespace,
            "labels": {"app.kubernetes.io/name": "ember", "app.kubernetes.io/component": "credentials"},
        },
        "stringData": {"username": settings.username, "token": token, "token_name": token_name},
        "type": "Opaque",
    }

    session = requests.Session()
    session.verify = ca_cert
    session.headers.update({"Authorization": f"Bearer {sa_token}", "Content-Type": "application/json"})

    put_url = f"{api_server}/api/v1/namespaces/{settings.namespace}/secrets/{settings.secret_name}"
    resp = session.put(put_url, json=payload)
    if resp.status_code == 404:
        post_url = f"{api_server}/api/v1/namespaces/{settings.namespace}/secrets"
        resp = session.post(post_url, json=payload)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create secret: {resp.status_code} {resp.text}")
        print("Secret created", flush=True)
    elif resp.status_code in (200, 201):
        print("Secret updated", flush=True)
    else:
        raise RuntimeError(f"Failed to upsert secret: {resp.status_code} {resp.text}")


def main() -> None:
    settings = load_settings()
    print("Waiting for Gitea API", flush=True)
    wait_for_gitea(settings.base_url)
    ensure_user(settings)
    token_name, token = create_token(settings)
    upsert_secret(settings, token_name, token)


if __name__ == "__main__":
    main()
