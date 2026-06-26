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
import time

import httpx

GROCY = "http://grocy.grocy-sf.svc.cluster.local:80"
ADMIN = "admin"  # Grocy's built-in admin user (created by the schema migration)
OPERATORS = ("agentydragon", "auragon")  # SF household humans to keep at ADMIN
ADMIN_PERMISSION_ID = 1  # the ADMIN root permission (implies all others)


def wait_for_schema(client: httpx.Client) -> None:
    """Await Grocy's schema. The app self-migrates at startup (postStart hits the
    auth-exempt `/`) and Flux only runs this Job once grocy is Ready, but poll
    defensively so the Job also works run standalone. Hit `/` (its body is an HTML
    redirect — ignore it) in case migration hasn't run, then poll the JSON
    `/api/users` until the schema exists.
    """
    for _ in range(30):
        with contextlib.suppress(httpx.HTTPError):
            client.get("/")
        resp = client.get("/api/users")
        if resp.status_code == 200 and isinstance(resp.json(), list):
            return
        time.sleep(2)
    raise RuntimeError("Grocy schema not ready in time")


def main() -> None:
    with httpx.Client(
        base_url=GROCY, headers={"X-authentik-username": ADMIN}, follow_redirects=True, timeout=30
    ) as client:
        wait_for_schema(client)
        for operator in OPERATORS:
            # Auto-create the operator if absent (reverse-proxy auth creates the user
            # on the first request bearing its username; default-deny means it is born
            # read-only), then grant ADMIN as the built-in admin user.
            client.get("/api/system/info", headers={"X-authentik-username": operator}).raise_for_status()
            users_resp = client.get("/api/users")
            users_resp.raise_for_status()
            user = next((u for u in users_resp.json() if u["username"] == operator), None)
            if user is None:
                raise RuntimeError(f"{operator!r} user was not created by the reverse-proxy auth")
            client.put(
                f"/api/users/{user['id']}/permissions", json={"permissions": [ADMIN_PERMISSION_ID]}
            ).raise_for_status()
            print(f"{operator!r} (id={user['id']}) ensured ADMIN")


if __name__ == "__main__":
    main()
