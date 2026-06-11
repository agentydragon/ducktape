"""Host-side MCP server for managing Gitea pull mirrors."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

import requests
from fastmcp.tools import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcp_infra.enhanced.server import EnhancedFastMCP


@dataclass
class MirrorConfig:
    base_url: str
    token: str

    @classmethod
    def resolve(cls, base_url: str | None, token: str | None) -> MirrorConfig:
        """Resolve config from parameters with environment variable fallback."""
        if not base_url:
            base_url = os.getenv("GITEA_BASE_URL")
        if not token:
            token = os.getenv("GITEA_TOKEN")

        if not base_url or not token:
            raise ValueError("Gitea mirror MCP requires GITEA_BASE_URL and GITEA_TOKEN")

        return cls(base_url=base_url, token=token)


class MirrorError(RuntimeError):
    pass


class TriggerMirrorSyncArgs(BaseModel):
    url: str
    model_config = ConfigDict(extra="forbid")


class TriggerMirrorSyncResponse(BaseModel):
    """Response matching Gitea's mirror-sync behavior (returns nothing)."""

    model_config = ConfigDict(extra="forbid")


class GetRepoInfoArgs(BaseModel):
    owner: str
    repo: str
    model_config = ConfigDict(extra="forbid")


class GiteaRepoInfo(BaseModel):
    """Base class containing all Gitea Repository fields.

    Shared by both the public response model and internal parsing model.
    """

    model_config = ConfigDict(extra="ignore")  # Lenient parsing - ignore unknown fields
    # Core repository identity
    id: int
    name: str = Field(description="Repository name. Mirror path for cloning: '{owner}/{name}.git'")
    full_name: str = Field(description="Full repository name including owner (owner/name)")
    description: str
    empty: bool
    private: bool
    fork: bool
    template: bool

    # Mirror-specific fields
    mirror: bool = Field(description="True if this repository is a pull mirror")
    mirror_updated: str = Field(
        description="ISO 8601 timestamp of last mirror update. Poll this endpoint and compare timestamps to detect sync completion."
    )
    mirror_interval: str = Field(description="Mirror sync interval (e.g., '8h0m0s')")

    # Repository metadata
    size: int = Field(description="Repository size in KB")
    language: str
    languages_url: str
    default_branch: str = Field(description="Default branch name (e.g., 'main', 'master')")
    archived: bool

    # URLs
    html_url: str
    url: str
    link: str
    ssh_url: str
    clone_url: str
    original_url: str
    website: str

    # Statistics
    stars_count: int
    forks_count: int
    watchers_count: int
    open_issues_count: int
    open_pr_counter: int
    release_counter: int

    # Timestamps
    created_at: str
    updated_at: str
    archived_at: str | None = None

    # Features
    has_code: bool
    has_issues: bool
    has_wiki: bool
    has_pull_requests: bool
    has_projects: bool
    projects_mode: str
    has_releases: bool
    has_packages: bool
    has_actions: bool
    ignore_whitespace_conflicts: bool

    # Merge settings
    allow_merge_commits: bool
    allow_rebase: bool
    allow_rebase_explicit: bool
    allow_squash_merge: bool
    allow_fast_forward_only_merge: bool
    allow_rebase_update: bool
    allow_manual_merge: bool
    autodetect_manual_merge: bool
    default_delete_branch_after_merge: bool
    default_merge_style: str
    default_allow_maintainer_edit: bool

    # Miscellaneous
    avatar_url: str
    internal: bool
    object_format_name: str
    topics: list[str]
    licenses: list[str]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"token {token}", "Accept": "application/json", "Content-Type": "application/json"}


def _post_json(url: str, token: str, payload: dict[str, Any] | None = None, *, timeout: int = 15) -> requests.Response:
    resp: requests.Response = requests.post(url, headers=_headers(token), json=payload or {}, timeout=timeout)
    return resp


class _UserInfo(BaseModel):
    login: str

    # The user payload includes many fields we do not consume; ignore them so schema changes
    # surface via targeted validation rather than mass field definitions.
    model_config = ConfigDict(extra="ignore")


def _get_typed_json[T_Model: BaseModel](
    url: str, token: str, model_type: type[T_Model], *, timeout: int = 15
) -> T_Model:
    resp = requests.get(url, headers=_headers(token), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):  # narrow type for mypy and correctness
        raise MirrorError("Expected JSON object from Gitea API")
    payload = cast(dict[str, Any], data)
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:  # pragma: no cover - exercised via tests
        raise MirrorError(f"Unexpected payload for {model_type.__name__}") from exc


def _slug_component(value: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in value)
    slug = slug.strip("-")
    return slug or "repo"


def _derive_repo_name(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"URL missing host for Gitea mirror: {url}")
    path = parsed.path.removesuffix(".git").strip("/")
    components = [parsed.netloc, *([p for p in path.split("/") if p])]
    return "-".join(_slug_component(part) for part in components)


def _ensure_mirror(cfg: MirrorConfig, upstream: str, owner: str, repo: str) -> None:
    migrate_url = f"{cfg.base_url.rstrip('/')}/api/v1/repos/migrate"
    payload = {"clone_addr": upstream, "repo_name": repo, "repo_owner": owner, "mirror": True, "private": False}
    resp = _post_json(migrate_url, cfg.token, payload)
    if resp.status_code not in (200, 201, 409):
        resp.raise_for_status()
        raise MirrorError(f"migrate failed ({resp.status_code}): {resp.text.strip()}")


def _trigger_sync(cfg: MirrorConfig, owner: str, repo: str) -> None:
    sync_url = f"{cfg.base_url.rstrip('/')}/api/v1/repos/{owner}/{repo}/mirror-sync"
    resp = _post_json(sync_url, cfg.token, {})
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        raise MirrorError(f"mirror-sync failed ({resp.status_code}): {resp.text.strip()}") from None


def _get_repo_info(cfg: MirrorConfig, owner: str, repo: str) -> GiteaRepoInfo:
    """Get current repository info including mirror status and last update time."""
    repo_url = f"{cfg.base_url.rstrip('/')}/api/v1/repos/{owner}/{repo}"
    try:
        data = _get_typed_json(repo_url, cfg.token, GiteaRepoInfo)
    except requests.RequestException as exc:
        raise MirrorError("failed to fetch repository metadata") from exc
    return data


def _resolve_owner(base_url: str, token: str) -> str:
    user_url = f"{base_url.rstrip('/')}/api/v1/user"
    data = _get_typed_json(user_url, token, _UserInfo)
    return data.login


class GiteaMirrorServer(EnhancedFastMCP):
    """Gitea mirror MCP server with typed tool access.

    Provides async pull mirror syncing for Gitea repositories.
    """

    # Tool references (assigned in __init__ after tool registration)
    trigger_mirror_sync_tool: FunctionTool
    get_repo_info_tool: FunctionTool

    def __init__(self, *, base_url: str | None = None, token: str | None = None):
        """Create a Gitea mirror MCP server.

        Configuration (env or kwargs):
          GITEA_BASE_URL: base URL to the Gitea instance (required)
          GITEA_TOKEN: access token with write:repository scope for target org/user (required)
        """
        cfg = MirrorConfig.resolve(base_url, token)

        super().__init__(
            "Gitea Mirror",
            instructions=(
                "Host-side Gitea mirror manager for async pull mirror syncing.\n\n"
                "Workflow:\n"
                "1. Call get_repo_info(owner, repo) to get initial mirror_updated timestamp\n"
                "2. Call trigger_mirror_sync(url) to start async sync (returns immediately)\n"
                "3. Poll get_repo_info(owner, repo) until mirror_updated timestamp changes\n"
                "4. When timestamps differ, sync is complete and mirror is ready for cloning\n\n"
                "Mirror path for cloning: '{owner}/{repo}.git'\n"
                "Typical sync time: 5-60 seconds depending on repository size. "
                "Recommended polling interval: 2-5 seconds."
            ),
        )

        # Register tools using clean pattern: tool name derived from function name
        def trigger_mirror_sync(input: TriggerMirrorSyncArgs) -> TriggerMirrorSyncResponse:
            """Ensure mirror exists and trigger async sync. Returns immediately.

            Matches Gitea's POST /repos/{owner}/{repo}/mirror-sync endpoint behavior.
            Creates a Gitea pull mirror if it doesn't exist, then triggers an async sync
            from the upstream repository.

            To detect sync completion: Call get_repo_info() before and after triggering sync,
            then poll until mirror_updated timestamp changes (typically 5-60 seconds).

            Returns: Empty response (matching Gitea API).
            """
            owner = _resolve_owner(cfg.base_url, cfg.token)
            repo = _derive_repo_name(input.url)
            _ensure_mirror(cfg, input.url, owner, repo)
            _trigger_sync(cfg, owner, repo)

            return TriggerMirrorSyncResponse()

        self.trigger_mirror_sync_tool = self.flat_model()(trigger_mirror_sync)

        def get_repo_info(input: GetRepoInfoArgs) -> GiteaRepoInfo:
            """Get repository information including mirror status.

            Matches Gitea's GET /repos/{owner}/{repo} endpoint (returns all fields).

            Use this to poll for sync completion after calling trigger_mirror_sync().
            Compare the returned mirror_updated timestamp before and after sync.
            When timestamps differ, the sync is complete.

            Recommended polling: Every 2-5 seconds until mirror_updated changes.
            """
            repo_data = _get_repo_info(cfg, input.owner, input.repo)

            if not repo_data.mirror:
                raise MirrorError(f"repository {input.owner}/{input.repo} is not a mirror")

            # Pass through all fields from Gitea API response
            return repo_data

        self.get_repo_info_tool = self.flat_model()(get_repo_info)
