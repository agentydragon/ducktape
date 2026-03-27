#!/usr/bin/env python3
"""Verify Authentik authentication works end-to-end.

Exercises the full challenge-response login flow against the Authentik API:
  1. GET  flow executor → identification challenge
  2. POST uid_field    → password challenge
  3. POST password     → redirect (success) or error

Retrieves the user password from kubectl and connects to Authentik via
kubectl port-forward. Exits 0 on success, 1 on failure.

Usage:
    python3 scripts/check-authentik-login.py
    python3 scripts/check-authentik-login.py --user akadmin --secret-ns authentik --secret-name authentik-admin-password --secret-key AUTHENTIK_BOOTSTRAP_PASSWORD
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import subprocess
import sys
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
    decoded = subprocess.run(["base64", "-d"], input=b64_value, capture_output=True, text=True, check=True)
    return decoded.stdout


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
    """Exercise the full Authentik challenge-response login flow.

    Returns True on successful login, False on failure.
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    flow_url = f"{base_url}/api/v3/flows/executor/{flow_slug}/"

    # Step 1: GET flow executor → identification challenge
    print(f"  GET {flow_url}")
    req = urllib.request.Request(flow_url, headers={"Accept": "application/json"})
    with opener.open(req) as resp:
        data = json.loads(resp.read())

    component = data.get("component", "")
    print(f"  Stage: {component}")
    if component != "ak-stage-identification":
        print(f"  ERROR: Expected ak-stage-identification, got {component}")
        print(f"  Response: {json.dumps(data, indent=2)}")
        return False

    # Step 2: POST uid_field → password challenge
    payload = json.dumps({"uid_field": uid}).encode()
    print(f"  POST uid_field={uid}")
    req = urllib.request.Request(
        flow_url, data=payload, headers={"Accept": "application/json", "Content-Type": "application/json"}
    )
    with opener.open(req) as resp:
        data = json.loads(resp.read())

    component = data.get("component", "")
    print(f"  Stage: {component}")
    if component != "ak-stage-password":
        print(f"  ERROR: Expected ak-stage-password, got {component}")
        print(f"  Response: {json.dumps(data, indent=2)}")
        return False

    # Step 3: POST password → redirect on success
    payload = json.dumps({"password": password}).encode()
    print("  POST password=***")
    req = urllib.request.Request(
        flow_url, data=payload, headers={"Accept": "application/json", "Content-Type": "application/json"}
    )
    try:
        with opener.open(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  ERROR: HTTP {e.code}: {body}")
        return False

    component = data.get("component", "")
    print(f"  Stage: {component}")

    if component == "xak-flow-redirect":
        return True

    # Could be another stage (e.g. MFA) or an error
    print(f"  ERROR: Unexpected component after password: {component}")
    print(f"  Response: {json.dumps(data, indent=2)}")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Authentik login flow")
    parser.add_argument("--user", default="agentydragon@gmail.com", help="Username or email to log in with")
    parser.add_argument(
        "--secret-ns", default="flux-system", help="Namespace of the K8s secret containing the password"
    )
    parser.add_argument("--secret-name", default="agentydragon-user-password", help="Name of the K8s secret")
    parser.add_argument("--secret-key", default="user_password", help="Key within the secret")
    parser.add_argument("--flow-slug", default="custom-authentication-flow", help="Authentik flow slug to exercise")
    parser.add_argument("--local-port", type=int, default=19080, help="Local port for kubectl port-forward")
    parser.add_argument("--url", default=None, help="Direct Authentik URL (skips port-forward)")
    args = parser.parse_args()

    # Get password
    print(f"Reading password from {args.secret_ns}/{args.secret_name}:{args.secret_key}...")
    password = get_password_from_k8s(args.secret_ns, args.secret_name, args.secret_key)
    if not password:
        print("ERROR: Empty password retrieved")
        sys.exit(1)
    print("  Password retrieved successfully")

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
        success = authentik_login(base_url, args.flow_slug, args.user, password)

        if success:
            print("LOGIN SUCCESSFUL")
            sys.exit(0)
        else:
            print("LOGIN FAILED")
            sys.exit(1)
    finally:
        if port_forward_proc:
            port_forward_proc.terminate()
            port_forward_proc.wait()


if __name__ == "__main__":
    main()
