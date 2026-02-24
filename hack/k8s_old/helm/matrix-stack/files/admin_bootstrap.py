#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass

import requests


@dataclass
class Settings:
    homeserver: str
    registration_secret: str
    username: str
    password: str


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


def load_settings() -> Settings:
    homeserver = env("SYNAPSE_HOMESERVER").rstrip("/")
    registration_secret = env("REGISTRATION_SHARED_SECRET")
    username = env("ADMIN_USERNAME")
    password = env("ADMIN_PASSWORD")
    return Settings(homeserver, registration_secret, username, password)


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


def register_admin(settings: Settings) -> None:
    session = requests.Session()
    nonce_resp = session.get(f"{settings.homeserver}/_synapse/admin/v1/register", timeout=5)
    nonce_resp.raise_for_status()
    nonce = nonce_resp.json()["nonce"]

    parts = [nonce, settings.username, settings.password, "admin"]
    mac_input = bytes([0]).join(part.encode() for part in parts)
    mac = hmac.new(settings.registration_secret.encode(), mac_input, hashlib.sha1).hexdigest()

    payload = {"nonce": nonce, "username": settings.username, "password": settings.password, "admin": True, "mac": mac}
    resp = session.post(f"{settings.homeserver}/_synapse/admin/v1/register", json=payload, timeout=10)
    if resp.status_code in (200, 201):
        print("Admin user created", flush=True)
        return
    if resp.status_code == 400:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        errcode = data.get("errcode")
        message = data.get("error", resp.text)
        if errcode in {"M_USER_IN_USE", "M_CONFLICT"} or "User ID already exists" in message:
            print("Admin user already exists (password unchanged)", flush=True)
            return
        raise RuntimeError(f"Failed to register admin user: {resp.status_code} {message}")
    resp.raise_for_status()


def main() -> None:
    settings = load_settings()
    print("Waiting for Synapse API", flush=True)
    wait_for_synapse(settings.homeserver)
    print("Synapse is ready", flush=True)
    register_admin(settings)


if __name__ == "__main__":
    main()
