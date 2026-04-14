"""Profile configuration loaded from a standalone YAML file.

Each profile (cli, web) lives under devinfra/claude/hook_daemon/profiles/<name>/profile.yaml.
The daemon loads exactly one profile at startup, selected by the
DUCKTAPE_CLAUDE_HOOKS_PROFILE env var.

Secrets are not handled here.
Web: sourced via startup_env_script (web_env.sh) at daemon startup (SOPS_AGE_KEY
is available in the daemon's inherited env from Claude Code).
CLI: sourced via .envrc (eval "$(devinfra/secrets/cli_env.sh)") before daemon starts.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class OtelConfig(BaseModel):
    endpoint: str | None = Field(default=None, description="OTLP/HTTP traces endpoint URL")
    bearer_token: str | None = Field(default=None, description="Bearer token for the OTLP endpoint")

    def with_env_overrides(self) -> OtelConfig:
        """Apply DUCKTAPE_CLAUDE_HOOKS_OTEL_* env var overrides.

        TODO: Rationalize env var override pattern — consider using pydantic-settings
        with a unified DUCKTAPE_CLAUDE_HOOKS_ prefix instead of ad-hoc os.environ.get().
        """
        return OtelConfig(
            endpoint=os.environ.get("DUCKTAPE_CLAUDE_HOOKS_OTEL_ENDPOINT", self.endpoint),
            bearer_token=os.environ.get("DUCKTAPE_CLAUDE_HOOKS_OTEL_AUTH_TOKEN", self.bearer_token),
        )


class K8sConfig(BaseModel):
    """K8s cluster connection config for kubeconfig generation."""

    server: str = Field(description="K8s API server URL")
    service_account: str = Field(description="ServiceAccount name for kubeconfig user and context")
    service_account_namespace: str = Field(default="default", description="Namespace of the ServiceAccount")
    namespace: str = Field(description="Default namespace for kubectl operations")


class BazelRemoteProxyConfig(BaseModel):
    target: str = Field(description="host:port to connect to, e.g. 'remote.buildbuddy.io:443'")


class BackgroundCommand(BaseModel):
    """Shell command to run in the background during session start."""

    name: str = Field(description="Human label for mailbox lifecycle messages")
    command: str = Field(description="Shell command passed to bash -c")
    timeout: int = Field(default=300, description="Seconds before the command is killed")
    after_env: bool = Field(
        default=False,
        description="If true, source the session env file before running and delay "
        "until after the env file is written. If false, run immediately.",
    )


class PreCommitConfig(BaseModel, frozen=True):
    """Pre-commit hook behavior configuration."""

    auto_apply_hooks: frozenset[str] = Field(
        default_factory=frozenset,
        description="Hook IDs whose file modifications are kept (not reverted). "
        "All other hooks' modifications are reverted and reported as diffs.",
    )
    show_report_diffs: bool = Field(
        default=False, description="Show unified diffs from report-only hooks in the PostToolUse output."
    )
    show_hook_output: bool = Field(
        default=False, description="Show stdout/stderr from failing hooks in the PostToolUse output."
    )


class GitShimConfig(BaseModel):
    """Per-behavior toggles for the git PATH shim.

    When the human shares the repo (CLI profile), block dangerous commands.
    When the agent owns the repo (web profile), let it do what it wants.
    """

    block_amend: bool = Field(default=False, description="Block `git commit --amend`.")
    block_stash: bool = Field(default=False, description="Block `git stash` (except list/show).")
    block_add_all: bool = Field(default=False, description="Block `git add -A`, `git add --all`, `git add .`.")


class ProfileConfig(BaseModel):
    bazel_remote_proxy: BazelRemoteProxyConfig | None = Field(
        default=None, description="UDS proxy for Bazel --remote_proxy (remote execution + cache). Null = disabled."
    )
    bazel_bes_proxy: BazelRemoteProxyConfig | None = Field(
        default=None,
        description="BES interceptor: gRPC service that inspects events and forwards to BuildBuddy. Null = disabled.",
    )
    bes_nudge_remote_execution: bool = Field(
        default=False,
        description="When BES interceptor is active, post a mailbox nudge if a build/test invocation "
        "lacks --remote_executor. Encourages agent to use `bb remote`.",
    )
    install_mkcert: bool = Field(default=False, description="Install mkcert and generate localhost TLS cert.")
    setup_docker: bool = Field(default=False, description="Set up Docker daemon under supervisor.")
    background_commands: list[BackgroundCommand] = Field(
        default_factory=list,
        description="Shell commands to run in the background during session start. "
        "Scripts can post messages via curl --unix-socket $HOOK_DAEMON_SOCK /mailbox.",
    )
    env_exports: str | None = Field(
        default=None, description="Inline shell content appended verbatim to the session env file."
    )
    setup_auth_proxy: bool = Field(
        default=False, description="Set up TLS-inspecting proxy (CA, truststore, combined bundle, UDS proxy)."
    )
    setup_tmpfs: bool = Field(
        default=False, description="Mount tmpfs for Docker storage and Bazel cache (useful on 9p/gVisor)."
    )
    idle_watchdog: bool = Field(
        description="Enable idle watchdog that shuts down daemon after inactivity. "
        "Disable in environments where the container is torn down externally."
    )

    # Formerly top-level HookConfig fields, now per-profile.
    k8s: K8sConfig | None = Field(default=None, description="K8s cluster connection config for kubeconfig generation.")
    otel: OtelConfig | None = Field(default=None, description="OpenTelemetry tracing configuration.")
    pre_commit: PreCommitConfig | None = Field(default=None, description="Pre-commit hook behavior configuration.")
    git_shim: GitShimConfig = Field(default_factory=GitShimConfig, description="Git shim behavior toggles.")
    startup_env_script: str | None = Field(
        default=None,
        description="Repo-relative path to a shell script run at daemon startup. "
        "The script's stdout must be eval-able shell (e.g. `export VAR=val` lines from try_export). "
        "New/changed vars are merged into os.environ before any session starts. "
        "Used by the web profile to decrypt SOPS secrets when SOPS_AGE_KEY is available.",
    )
    context_template: str | None = Field(
        default=None, description="Repo-relative path to a Mako template rendered into the session context output."
    )

    @classmethod
    def load(cls, config_path: Path) -> ProfileConfig:
        """Load profile config from a standalone YAML file."""
        raw = yaml.safe_load(config_path.read_text())
        config = cls.model_validate(raw)
        if config.otel:
            config = config.model_copy(update={"otel": config.otel.with_env_overrides()})
        return config
