"""Provision the read-only `haku` Grocy user in the grocy-sf instance.

Grocy's REST API enforces per-user permissions: reads require none, every write
requires a specific permission (verified empirically: a user with no permissions
gets 200 on reads and 403 on writes). Permissions are assigned only at user
creation (default `ADMIN`) and never re-asserted on login, so stripping them once
sticks. Therefore a read-only user is simply a user with an empty permission set.

Idempotent: ensures the `haku` user exists, then sets its permissions to []. Runs
as the built-in `admin` user via Grocy's trusted `X-authentik-username` header —
the dedicated NetworkPolicy is what restricts who may present it (the pod talks to
Grocy in-cluster, bypassing the Authentik outpost).
"""

import json
import time
import urllib.error
import urllib.request

GROCY = "http://grocy.grocy-sf.svc.cluster.local:80"
ADMIN = "admin"  # Grocy's built-in admin user (created by the schema migration)
HAKU = "haku"


def call(method: str, path: str, *, user: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-authentik-username": user}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(GROCY + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as response:
        raw = response.read().decode()
    return json.loads(raw) if raw.strip() else None


def wait_for_migration() -> None:
    """Trigger and await Grocy's schema migration.

    Migration runs on the first request to a content route, NOT on the `/login`
    readiness probe — a freshly-provisioned PVC has a 0-byte DB until something
    hits `/`. Until the schema exists, the auth middleware errors on the missing
    `users` table, so poll `/api/users` (as admin) until it returns a list.
    """
    for _ in range(30):
        try:
            call("GET", "/", user=ADMIN)  # 302 to login; side effect: run migration
            if isinstance(call("GET", "/api/users", user=ADMIN), list):
                return
        except urllib.error.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError("Grocy schema migration did not complete in time")


def main() -> None:
    wait_for_migration()

    # Auto-create `haku` if absent: reverse-proxy auth creates the user on the
    # first request bearing its username; a GET needs no permission.
    call("GET", "/api/system/info", user=HAKU)

    users = call("GET", "/api/users", user=ADMIN)
    if not isinstance(users, list):
        raise RuntimeError(f"GET /api/users did not return a list: {users!r}")
    haku = next((u for u in users if u["username"] == HAKU), None)
    if haku is None:
        raise RuntimeError(f"{HAKU!r} user was not created by the reverse-proxy auth")

    call("PUT", f"/api/users/{haku['id']}/permissions", user=ADMIN, body={"permissions": []})

    perms = call("GET", f"/api/users/{haku['id']}/permissions", user=ADMIN)
    if perms:
        raise RuntimeError(f"expected empty permissions for {HAKU!r}, got {perms}")
    print(f"{HAKU!r} user (id={haku['id']}) provisioned read-only (permissions=[])")


if __name__ == "__main__":
    main()
