"""Ensure the SF household operator(s) have ADMIN in the grocy-sf instance.

Read-only is the default here (DEFAULT_PERMISSIONS=none, see ../app), so haku and
any other auto-created user is read-only. This one-shot Job does the single explicit
elevation default-deny requires: ensure each human operator exists and has ADMIN.
Flux runs it once after grocy is up (dependsOn grocy-sf), so a from-scratch cluster
rebuild ends with the operators able to use Grocy — no manual step.

Idempotent. Runs as the built-in `admin` user (always ADMIN, created by the schema
migration) via Grocy's trusted X-authentik-username header; the sibling
NetworkPolicy is what restricts who may present it.
"""

import contextlib
import json
import time
import urllib.error
import urllib.request

GROCY = "http://grocy.grocy-sf.svc.cluster.local:80"
ADMIN = "admin"  # Grocy's built-in admin user (created by the schema migration)
OPERATORS = ("agentydragon", "auragon")  # SF household humans to keep at ADMIN
ADMIN_PERMISSION_ID = 1  # the ADMIN root permission (implies all others)


def call(method: str, path: str, *, user: str, body: dict | None = None):
    """JSON API request. Raises HTTPError on non-2xx; expects a JSON (or empty) body."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-authentik-username": user}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(GROCY + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as response:
        raw = response.read().decode()
    return json.loads(raw) if raw.strip() else None


def wait_for_schema() -> None:
    """Await Grocy's schema. The app self-migrates at startup (postStart hits the
    auth-exempt `/`) and Flux only runs this Job once grocy is Ready, but poll
    defensively so the Job also works run standalone. Hit `/` raw (its body is an
    HTML redirect — never parse it) in case migration hasn't run, then poll the
    JSON `/api/users` until the schema exists.
    """
    for _ in range(30):
        with contextlib.suppress(urllib.error.HTTPError):
            urllib.request.urlopen(
                urllib.request.Request(GROCY + "/", headers={"X-authentik-username": ADMIN}, method="GET")
            ).read()
        try:
            if isinstance(call("GET", "/api/users", user=ADMIN), list):
                return
        except urllib.error.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError("Grocy schema not ready in time")


def main() -> None:
    wait_for_schema()
    for operator in OPERATORS:
        # Auto-create the operator if absent (reverse-proxy auth creates the user on
        # the first request bearing its username; default-deny means it is born
        # read-only), then grant ADMIN as the built-in admin user.
        call("GET", "/api/system/info", user=operator)
        users = call("GET", "/api/users", user=ADMIN)
        if not isinstance(users, list):
            raise RuntimeError(f"GET /api/users did not return a list: {users!r}")
        user = next((u for u in users if u["username"] == operator), None)
        if user is None:
            raise RuntimeError(f"{operator!r} user was not created by the reverse-proxy auth")
        call("PUT", f"/api/users/{user['id']}/permissions", user=ADMIN, body={"permissions": [ADMIN_PERMISSION_ID]})
        print(f"{operator!r} (id={user['id']}) ensured ADMIN")


if __name__ == "__main__":
    main()
