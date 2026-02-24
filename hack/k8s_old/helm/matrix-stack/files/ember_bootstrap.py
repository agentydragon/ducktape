#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass

import requests
from kubernetes import client, config
from kubernetes.client import ApiException


@dataclass
class Settings:
    homeserver: str
    registration_secret: str
    username: str
    password: str
    namespace: str
    secret_name: str


def env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Environment variable {key} must be set")
    return value


def load_settings() -> Settings:
    homeserver = env("SYNAPSE_HOMESERVER").rstrip("/")
    registration_secret = env("REGISTRATION_SHARED_SECRET")
    username = env("EMBER_USERNAME")
    password = env("EMBER_PASSWORD")
    namespace = env("EMBER_NAMESPACE")
    secret_name = env("EMBER_SECRET_NAME")
    return Settings(homeserver, registration_secret, username, password, namespace, secret_name)


def wait_for_synapse(homeserver: str, timeout_seconds: int = 300) -> None:
    url = f"{homeserver}/_matrix/client/versions"
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=5)
            if response.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    raise RuntimeError("Synapse did not become ready in time")


def register_user(settings: Settings) -> None:
    session = requests.Session()
    nonce_resp = session.get(f"{settings.homeserver}/_synapse/admin/v1/register", timeout=5)
    nonce_resp.raise_for_status()
    nonce = nonce_resp.json()["nonce"]

    parts = [nonce, settings.username, settings.password, "notadmin"]
    mac_input = bytes([0]).join(part.encode() for part in parts)
    mac = hmac.new(settings.registration_secret.encode(), mac_input, hashlib.sha1).hexdigest()

    payload = {"nonce": nonce, "username": settings.username, "password": settings.password, "admin": False, "mac": mac}
    resp = session.post(f"{settings.homeserver}/_synapse/admin/v1/register", json=payload, timeout=5)
    if resp.status_code in (200, 201):
        print("Matrix user created", flush=True)
        return
    if resp.status_code == 400:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        errcode = data.get("errcode")
        message = data.get("error", resp.text)
        if errcode in {"M_USER_IN_USE", "M_CONFLICT"} or "User ID already exists" in message:
            print("Matrix user already exists", flush=True)
            return
        raise RuntimeError(f"Failed to register Matrix user: {resp.status_code} {message}")
    resp.raise_for_status()


def login(settings: Settings) -> str:
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": settings.username},
        "password": settings.password,
    }
    for _ in range(10):
        response = requests.post(f"{settings.homeserver}/_matrix/client/v3/login", json=payload, timeout=10)
        if response.status_code == 429:
            try:
                retry_ms = int(response.json().get("retry_after_ms", 1000))
            except (ValueError, KeyError):
                retry_ms = 1000
            wait_s = max(retry_ms / 1000.0, 1)
            print(f"Matrix login throttled, retrying in {wait_s:.1f}s", flush=True)
            time.sleep(wait_s)
            continue
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"Matrix login failed: {response.status_code} {response.text}") from exc
        token = response.json().get("access_token")
        if not token:
            raise RuntimeError("Matrix login did not return an access token")
        print("Obtained Matrix access token", flush=True)
        return token
    raise RuntimeError("Matrix login exceeded retry limit")


def upsert_secret(settings: Settings, token: str) -> None:
    config.load_incluster_config()
    api = client.CoreV1Api()

    metadata = client.V1ObjectMeta(
        name=settings.secret_name,
        namespace=settings.namespace,
        labels={"app.kubernetes.io/name": "ember", "app.kubernetes.io/component": "credentials"},
    )
    body = client.V1Secret(string_data={"access_token": token}, metadata=metadata, type="Opaque")

    try:
        api.replace_namespaced_secret(settings.secret_name, settings.namespace, body)
        print("Secret updated", flush=True)
    except ApiException as exc:
        if exc.status == 404:
            api.create_namespaced_secret(settings.namespace, body)
            print("Secret created", flush=True)
        else:
            raise


def main() -> None:
    settings = load_settings()
    print("Waiting for Synapse API", flush=True)
    wait_for_synapse(settings.homeserver)
    print("Synapse is ready", flush=True)
    register_user(settings)
    token = login(settings)
    upsert_secret(settings, token)


if __name__ == "__main__":
    main()
