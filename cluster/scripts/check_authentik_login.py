#!/usr/bin/env python3
"""Check Authentik's native username/password login flow.

Exercises the full challenge-response login flow against the Authentik API:
  1. GET  flow executor → identification challenge
  2. POST uid_field    → password challenge
  3. POST password     → redirect (success) or error

Retrieves the user password from kubectl and connects to Authentik via
kubectl port-forward. Exits 0 on success, 1 on failure.

Usage:
    bb run //cluster/scripts:check_authentik_login
    bb run //cluster/scripts:check_authentik_login -- --user akadmin --secret-name authentik-admin-password --secret-key AUTHENTIK_BOOTSTRAP_PASSWORD
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
import urllib.error
import urllib.request


def get_password_from_k8s(namespace: str, secret_name: str, secret_key: str) -> str:
    """Read a password from a Kubernetes secret."""
    result = subprocess.run(
        ["kubectl", "get", "secret", "-n", namespace, secret_name, "-o", f"jsonpath={{.data.{secret_key}}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    b64_value = result.stdout.strip()
    if not b64_value:
        raise RuntimeError(f"Empty value for {namespace}/{secret_name}:{secret_key}")
    return base64.b64decode(b64_value).decode()


def start_port_forward(namespace: str, service: str, local_port: int, remote_port: int) -> subprocess.Popen:
    """Start kubectl port-forward in the background."""
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, f"svc/{service}", f"{local_port}:{remote_port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for port-forward to be ready
    time.sleep(2)
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(f"port-forward exited immediately: {stderr}")
    return proc


def authentik_login(base_url: str, flow_slug: str, uid: str, password: str) -> bool:
    """Advance through the password flow using one cookie session."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    flow_url = f"{base_url}/api/v3/flows/executor/{flow_slug}/"
    print(f"  Flow executor: {flow_url}")
    for payload, expected_component in (
        (None, "ak-stage-identification"),
        ({"uid_field": uid}, "ak-stage-password"),
        ({"password": password}, "xak-flow-redirect"),
    ):
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        req = urllib.request.Request(flow_url, data=body, headers=headers)
        try:
            with opener.open(req, timeout=30) as resp:
                component = json.load(resp).get("component")
        except urllib.error.HTTPError as exc:
            print(f"  ERROR: HTTP {exc.code} {exc.reason} waiting for {expected_component}")
            return False
        if component != expected_component:
            print(f"  ERROR: Expected {expected_component}, got {component}")
            return False
        print(f"  Stage: {component}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Native user and password Secret are wired by tf/gitops/sso-providers/main.tf.
    parser.add_argument("--user", default="agentydragon", help="Username or email to log in with")
    parser.add_argument("--secret-ns", default="authentik", help="Namespace of the K8s secret containing the password")
    parser.add_argument("--secret-name", default="authentik-user-password", help="Name of the K8s secret")
    parser.add_argument("--secret-key", default="USER_PASSWORD", help="Key within the secret")
    parser.add_argument("--flow-slug", default="default-authentication-flow", help="Authentik flow slug to exercise")
    parser.add_argument("--local-port", type=int, default=19080, help="Local port for kubectl port-forward")
    parser.add_argument("--url", default=None, help="Direct Authentik URL (skips port-forward)")
    args = parser.parse_args()

    # Get password
    print(f"Reading password from {args.secret_ns}/{args.secret_name}:{args.secret_key}...")
    password = get_password_from_k8s(args.secret_ns, args.secret_name, args.secret_key)

    # Set up connectivity
    port_forward_proc = None
    if args.url:
        base_url = args.url.rstrip("/")
    else:
        print(f"Starting port-forward to authentik-server on localhost:{args.local_port}...")
        port_forward_proc = start_port_forward("authentik", "authentik-server", args.local_port, 80)
        base_url = f"http://localhost:{args.local_port}"

    try:
        print(f"Exercising login flow ({args.flow_slug}) as {args.user}...")
        if not authentik_login(base_url, args.flow_slug, args.user, password):
            print("LOGIN FAILED")
            raise SystemExit(1)
        print("LOGIN SUCCESSFUL")
    finally:
        if port_forward_proc:
            port_forward_proc.terminate()
            port_forward_proc.wait()


if __name__ == "__main__":
    main()
