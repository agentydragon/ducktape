from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, urlencode

import requests
from pydantic import BaseModel, Field

from ember.secrets import ProjectedSecret

__all__ = ["GiteaBranchInfo", "GiteaClient", "GiteaComment", "GiteaError", "GiteaRepository", "GiteaUser"]


class GiteaError(RuntimeError):
    """Raised when the Gitea API returns an error."""


class GiteaRepository(BaseModel):
    owner: str
    name: str

    @classmethod
    def parse(cls, value: str | GiteaRepository) -> GiteaRepository:
        if isinstance(value, cls):
            return value
        assert isinstance(value, str)  # Type narrowing for mypy
        if "/" not in value:
            raise ValueError("Gitea repository must be in 'owner/name' format")
        owner, name = value.split("/", 1)
        if not owner or not name:
            raise ValueError("Invalid Gitea repository value")
        return cls(owner=owner, name=name)

    @property
    def api_path(self) -> str:
        return f"{self.owner}/{self.name}"


class GiteaUser(BaseModel):
    login: str
    username: str | None = None

    @property
    def handle(self) -> str:
        return self.username or self.login


class GiteaComment(BaseModel):
    id: int
    body: str
    user: GiteaUser
    created_at: str | None = None


class GiteaBranchInfo(BaseModel):
    name: str
    commit: dict[str, Any] = Field(default_factory=dict)

    @property
    def sha(self) -> str:
        value = self.commit.get("sha")
        if not isinstance(value, str):
            raise GiteaError("Branch commit missing sha")
        return value


class GiteaClient:
    def __init__(self, *, base_url: str, token: str, default_repo: GiteaRepository | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._default_repo = default_repo
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"token {token}", "Accept": "application/json"})

    @classmethod
    def from_projected_secret(
        cls, *, base_url: str, repo: str | GiteaRepository | None, secret: ProjectedSecret
    ) -> GiteaClient:
        token = secret.value(required=True)
        repository = GiteaRepository.parse(repo) if repo else None
        return cls(base_url=base_url, token=token or "", default_repo=repository)

    def with_repo(self, repo: str | GiteaRepository | None) -> GiteaClient:
        repository = GiteaRepository.parse(repo) if repo else self._default_repo
        return GiteaClient(base_url=self._base_url, token=self._token, default_repo=repository)

    def issue_comments(self, issue: int, repo: str | GiteaRepository | None = None) -> list[GiteaComment]:
        repository = self._resolve_repo(repo)
        url = self._build_url(f"/api/v1/repos/{repository.api_path}/issues/{issue}/comments")
        data = self._request_json("GET", url)
        return [GiteaComment.model_validate(item) for item in data]

    def branch_info(self, branch: str, repo: str | GiteaRepository | None = None) -> GiteaBranchInfo:
        repository = self._resolve_repo(repo)
        encoded_branch = quote(branch, safe="")
        url = self._build_url(f"/api/v1/repos/{repository.api_path}/branches/{encoded_branch}")
        data = self._request_json("GET", url)
        return GiteaBranchInfo.model_validate(data)

    def file_contents(self, path: str, ref: str, repo: str | GiteaRepository | None = None) -> str:
        repository = self._resolve_repo(repo)
        encoded_path = quote(path.lstrip("/"), safe="/")
        url = self._build_url(f"/api/v1/repos/{repository.api_path}/contents/{encoded_path}", query={"ref": ref})
        data = self._request_json("GET", url)
        content = data.get("content")
        encoding = data.get("encoding", "")
        if not isinstance(content, str):
            raise GiteaError(f"File {path} response missing content")
        if encoding == "base64":
            return base64.b64decode(content).decode("utf-8")
        return content

    def _resolve_repo(self, repo: str | GiteaRepository | None) -> GiteaRepository:
        if repo is None:
            if not self._default_repo:
                raise GiteaError("No repository specified")
            return self._default_repo
        return GiteaRepository.parse(repo)

    def _build_url(self, path: str, query: dict[str, str] | None = None) -> str:
        path_fragment = path if path.startswith("/") else f"/{path}"
        if query:
            return f"{self._base_url}{path_fragment}?{urlencode(query)}"
        return f"{self._base_url}{path_fragment}"

    def _request_json(self, method: str, url: str) -> Any:
        response = self._session.request(method, url, timeout=30)
        if response.status_code >= 400:
            raise GiteaError(f"Gitea API {method} {url} failed: {response.status_code} {response.text}")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise GiteaError(f"Failed to decode JSON from {url}") from exc
