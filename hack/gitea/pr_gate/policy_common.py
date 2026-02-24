import json
import os
import re
import time

# Configuration via environment (shared)
GITEA_BASE = os.environ.get("GITEA_BASE_URL", "http://127.0.0.1:3000/").rstrip("/") + "/"
ADMIN_TOKEN = os.environ.get("GITEA_ADMIN_TOKEN", "").strip()
DEFAULT_LIMIT = int(os.environ.get("PRQ_DEFAULT_MAX", "3"))

try:
    PER_REPO_LIMITS = json.loads(os.environ.get("PRQ_PER_REPO", "{}"))
except json.JSONDecodeError:
    PER_REPO_LIMITS = {}

EXEMPT_USERS = {u.strip().lower() for u in os.environ.get("PRQ_EXEMPT_USERS", "").split(",") if u.strip()}

API_TIMEOUT = float(os.environ.get("PRQ_API_TIMEOUT_SECS", "5"))
CACHE_TTL = float(os.environ.get("PRQ_CACHE_TTL_SECS", "2"))

# Precompiled regex patterns with named groups
RE_OWNER_REPO_PULLS_OR_COMPARE = re.compile(
    r"^/(?:api/v1/repos/)?(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:pulls|compare)(?:[/?].*)?$"
)
RE_PULL_INDEX_OPTIONAL_STATUS = re.compile(
    r"^/(?:api/v1/repos/)?(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<index>\d+)(?:/status)?(?:[/?].*)?$"
)


def user_headers(cookie: str = "", authz: str = "", admin: bool = False) -> dict[str, str]:
    if admin and ADMIN_TOKEN:
        return {"Authorization": f"token {ADMIN_TOKEN}"}
    hdrs: dict[str, str] = {}
    if cookie:
        hdrs["Cookie"] = cookie
    if authz:
        hdrs["Authorization"] = authz
    return hdrs


def parse_owner_repo_from_uri(uri: str) -> tuple[str, str] | None:
    if m := RE_OWNER_REPO_PULLS_OR_COMPARE.match(uri):
        g = m.groupdict()
        owner = g.get("owner")
        repo = g.get("repo")
        if owner and repo:
            return owner, repo
    return None


def parse_reopen_targets(uri: str) -> tuple[str, str, int] | None:
    if m := RE_PULL_INDEX_OPTIONAL_STATUS.match(uri):
        g = m.groupdict()
        owner = g.get("owner")
        repo = g.get("repo")
        index = g.get("index")
        if owner and repo and index:
            return owner, repo, int(index)
    return None


def get_limit_for_repo(owner: str, repo: str) -> int:
    return int(PER_REPO_LIMITS.get(f"{owner}/{repo}", DEFAULT_LIMIT))


def normalize_login(login: str) -> str:
    return (login or "").lower()


class PRCountCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self.store: dict[tuple[str, str, str], tuple[float, int]] = {}

    def get(self, owner: str, repo: str, author: str) -> int | None:
        key = (owner, repo, normalize_login(author))
        now = time.monotonic()
        if (entry := self.store.get(key)) and entry[0] > now:
            return entry[1]
        return None

    def set(self, owner: str, repo: str, author: str, count: int) -> None:
        key = (owner, repo, normalize_login(author))
        self.store[key] = (time.monotonic() + self.ttl, count)


CACHE = PRCountCache(CACHE_TTL)
