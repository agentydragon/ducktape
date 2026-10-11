"""Async GitHub App client: transport, authentication and typed REST resources.

No webhook, database or subscription scheduling ownership. Rate limits are surfaced to the
caller immediately so the notification service can persist backoff rather than sleep here.
"""

import asyncio
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

from agentplane.notification_service.settings import GitHubSettings
from agentplane.notification_service.sources.github.models import RepositoryName

# RS256 is loaded dynamically by PyJWT.
# gazelle:include_dep @pypi//cryptography


class GitHubUnavailableError(Exception):
    """Disabled provider or confirmed loss of repository/installation access."""


class GitHubAccessError(GitHubUnavailableError):
    """Access is denied or the installation is suspended."""


class GitHubSourceChangedError(GitHubUnavailableError):
    """The authorized repository, installation or commit identity changed."""


class GitHubNotInstalledError(GitHubAccessError):
    """The App's installation lookup returned 404 for this repository."""


class GitHubRetryError(Exception):
    def __init__(self, status_code: int, retry_seconds: int) -> None:
        self.retry_seconds = retry_seconds
        super().__init__(f"GitHub rate limited (HTTP {status_code}); retry in {retry_seconds}s")


def rate_limit_delay(headers: httpx.Headers, now: float) -> int:
    # GitHub primary limits use an epoch reset; secondary limits may supply Retry-After.
    # Neither deadline may be shortened by our fallback or a maximum-delay clamp.
    delay = 60
    retry_after = headers.get("retry-after", "")
    if retry_after.isascii() and retry_after.isdecimal():
        delay = max(delay, int(retry_after))
    elif retry_after:
        try:
            deadline = parsedate_to_datetime(retry_after)
        except ValueError, OverflowError:
            pass
        else:
            if deadline.tzinfo is not None:
                delay = max(delay, math.ceil(deadline.timestamp() - now))
    reset = headers.get("x-ratelimit-reset", "")
    if headers.get("x-ratelimit-remaining") == "0" and reset.isascii() and reset.isdecimal():
        delay = max(delay, math.ceil(int(reset) - now))
    return delay


class Upstream(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class Installation(Upstream):
    id: int = Field(gt=0)
    suspended_at: datetime | None = None


class Repository(Upstream):
    id: int = Field(gt=0)
    full_name: RepositoryName


class Revision(Upstream):
    sha: str
    repo: Repository | None = None


class PullRequest(Upstream):
    number: int
    head: Revision


class Issue(Upstream):
    number: int
    pull_request: dict[str, JsonValue] | None = None


class GitRef(Upstream):
    object: Revision


class Commit(Upstream):
    sha: str


class Token(Upstream):
    token: SecretStr
    expires_at: datetime


def api_headers(bearer: str, api_version: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": api_version,
    }


class GitHubClient:
    def __init__(self, http: httpx.AsyncClient, settings: GitHubSettings) -> None:
        self.http, self.settings = http, settings
        self.tokens: dict[int, Token] = {}
        self.token_lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def open(cls, settings: GitHubSettings) -> AsyncIterator[GitHubClient]:
        async with httpx.AsyncClient(
            base_url=str(settings.api_url), timeout=settings.request_timeout_s, follow_redirects=False
        ) as http:
            client = cls(http, settings)
            client.start()
            yield client

    def app_headers(self) -> dict[str, str]:
        private_key = self.settings.private_key.get_secret_value()
        now = int(time.time())
        bearer = jwt.encode(
            {"iat": now - 30, "exp": now + 540, "iss": str(self.settings.app_id)}, private_key, algorithm="RS256"
        )
        return api_headers(bearer, self.settings.api_version.isoformat())

    def start(self) -> None:
        # Validate the App private key before HTTP readiness; Settings validates the signing secret.
        self.app_headers()

    async def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        *,
        json: dict[str, JsonValue] | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response:
        # No redirects: credentials must never follow repository redirects to another origin.
        response = await self.http.request(method, path, headers=headers, json=json, follow_redirects=False)
        if response.status_code == 429 or (
            response.status_code == 403
            and (response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers)
        ):
            raise GitHubRetryError(response.status_code, rate_limit_delay(response.headers, time.time()))
        if allow_missing and response.status_code == 404:
            return response
        if response.status_code == 401:
            self.tokens.clear()
        if response.status_code in (401, 403, 404):
            raise GitHubAccessError(
                f"GitHub App access unavailable (HTTP {response.status_code}); check credentials and permissions"
            )
        response.raise_for_status()
        return response

    async def installation_headers(self, installation_id: int) -> dict[str, str]:
        async with self.token_lock:
            token = self.tokens.get(installation_id)
            if token is None or token.expires_at <= datetime.now(UTC) + timedelta(minutes=5):
                response = await self.request(
                    "POST",
                    f"/app/installations/{installation_id}/access_tokens",
                    self.app_headers(),
                    json={
                        "permissions": {
                            "metadata": "read",
                            "contents": "read",
                            "pull_requests": "read",
                            "issues": "read",
                        }
                    },
                )
                token = Token.model_validate_json(response.content)
                self.tokens[installation_id] = token
        return api_headers(token.token.get_secret_value(), self.settings.api_version.isoformat())

    async def installation(self, repository: str) -> Installation:
        response = await self.request(
            "GET", f"/repos/{repository}/installation", self.app_headers(), allow_missing=True
        )
        if response.status_code == 404:
            raise GitHubNotInstalledError("GitHub App has no accessible installation for this repository")
        installation = Installation.model_validate_json(response.content)
        if installation.suspended_at is not None:
            raise GitHubAccessError("GitHub App installation is suspended")
        return installation

    async def repository(self, name: str, installation_id: int) -> Repository:
        headers = await self.installation_headers(installation_id)
        return Repository.model_validate_json((await self.request("GET", f"/repos/{name}", headers)).content)

    async def repository_by_id(self, repository_id: int, installation_id: int) -> Repository:
        headers = await self.installation_headers(installation_id)
        return Repository.model_validate_json(
            (await self.request("GET", f"/repositories/{repository_id}", headers)).content
        )

    async def pull_request(self, repository: str, number: int, installation_id: int) -> PullRequest:
        headers = await self.installation_headers(installation_id)
        return PullRequest.model_validate_json(
            (await self.request("GET", f"/repos/{repository}/pulls/{number}", headers)).content
        )

    async def issue(self, repository: str, number: int, installation_id: int) -> Issue:
        headers = await self.installation_headers(installation_id)
        issue = Issue.model_validate_json(
            (await self.request("GET", f"/repos/{repository}/issues/{number}", headers)).content
        )
        if issue.number != number or issue.pull_request is not None:
            raise GitHubSourceChangedError("GitHub issue identity changed or refers to a pull request")
        return issue

    async def branch(self, repository: str, name: str, installation_id: int) -> GitRef | None:
        headers = await self.installation_headers(installation_id)
        response = await self.request(
            "GET", f"/repos/{repository}/git/ref/heads/{quote(name, safe='')}", headers, allow_missing=True
        )
        return None if response.status_code == 404 else GitRef.model_validate_json(response.content)

    async def commit(self, repository: str, sha: str, installation_id: int) -> Commit:
        headers = await self.installation_headers(installation_id)
        commit = Commit.model_validate_json(
            (await self.request("GET", f"/repos/{repository}/commits/{sha}", headers)).content
        )
        if commit.sha != sha:
            raise GitHubSourceChangedError("GitHub commit identity changed")
        return commit
