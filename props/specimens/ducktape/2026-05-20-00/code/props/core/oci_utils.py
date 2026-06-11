"""Utilities for OCI image operations.

Handles:
- Registry configuration (RegistryProxyConfig, UpstreamRegistryConfig)
- OCI reference building
- Digest detection
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass

from props.core.agent_types import AgentType

logger = logging.getLogger(__name__)

# Builtin image tag - used by all Bazel oci_push targets
BUILTIN_TAG = "latest"


@dataclass(frozen=True)
class RegistryProxyConfig:
    """Registry proxy configuration for image resolution and OCI references.

    The registry proxy is part of the props backend - it proxies OCI API requests
    to an upstream registry and records agent_definitions on push.

    host/port: How the backend reaches the registry proxy (HTTP tag resolution).
    pull_host/pull_port: How the container runtime (kubelet/Docker) pulls images.
      Defaults to host/port when not set. Needed in k8s where the backend resolves
      the service name (e.g. "props") via cluster DNS, but the kubelet can't.
    project: Optional upstream registry project prefix (e.g. "props" for Harbor).
      When set, OCI references include it: registry.allegedly.works/props/critic@...
    """

    host: str
    port: int
    pull_host: str | None = None
    pull_port: int | None = None
    project: str | None = None

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def pull_authority(self) -> str:
        """Host:port string for image references (what the container runtime pulls from)."""
        h = self.pull_host or self.host
        p = self.pull_port if self.pull_host else self.port
        if p is None or p in (443, 80):
            return h
        return f"{h}:{p}"

    def build_oci_reference(self, agent_type: AgentType, digest: str) -> str:
        """Build full OCI reference (authority/repository@digest)."""
        repo = str(agent_type)
        if self.project:
            repo = f"{self.project}/{repo}"
        return f"{self.pull_authority()}/{repo}@{digest}"


def get_registry_proxy_config() -> RegistryProxyConfig:
    """Get registry configuration from environment variables.

    Environment variables:
        PROPS_REGISTRY_HOST: Host for backend to reach registry proxy (default: 127.0.0.1)
        PROPS_REGISTRY_PORT: Port for backend to reach registry proxy (default: 8000)
        PROPS_REGISTRY_PULL_HOST: Host for container runtime image pulls (default: PROPS_REGISTRY_HOST)
        PROPS_REGISTRY_PULL_PORT: Port for container runtime image pulls (default: PROPS_REGISTRY_PORT)
        PROPS_REGISTRY_UPSTREAM_PROJECT: Harbor project prefix for OCI references (default: none)
    """
    pull_host = os.environ.get("PROPS_REGISTRY_PULL_HOST") or None
    pull_port_str = os.environ.get("PROPS_REGISTRY_PULL_PORT")
    pull_port = int(pull_port_str) if pull_port_str else None
    project = os.environ.get("PROPS_REGISTRY_UPSTREAM_PROJECT") or None
    return RegistryProxyConfig(
        host=os.environ.get("PROPS_REGISTRY_HOST", "127.0.0.1"),
        port=int(os.environ.get("PROPS_REGISTRY_PORT", "8000")),
        pull_host=pull_host,
        pull_port=pull_port,
        project=project,
    )


@dataclass(frozen=True)
class UpstreamRegistryConfig:
    """Config for the upstream registry the proxy forwards to.

    Consolidates all upstream env vars in one place so neither registry.py
    nor any other module reads them directly.

    Environment variables:
        PROPS_REGISTRY_UPSTREAM_URL: Base URL of upstream registry (required)
        PROPS_REGISTRY_UPSTREAM_USERNAME: Username for upstream auth (optional)
        PROPS_REGISTRY_UPSTREAM_PASSWORD: Password for upstream auth (optional)
        PROPS_REGISTRY_UPSTREAM_PROJECT: Harbor project prefix, e.g. "props" (optional)
    """

    url: str
    username: str | None
    password: str | None
    project: str | None

    def auth_header(self) -> str | None:
        """HTTP Basic auth header for upstream requests, or None if unconfigured."""
        if self.username and self.password:
            creds = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            return f"Basic {creds}"
        return None

    def rewrite_path(self, path: str) -> str:
        """Prepend project to path: /v2/critic/... → /v2/props/critic/..."""
        if self.project and path.startswith("/v2/") and path != "/v2/":
            return f"/v2/{self.project}/{path[4:]}"
        return path

    def repo_path(self, repo: str) -> str:
        """Prefix repo with project if configured: 'critic' → 'props/critic'."""
        return f"{self.project}/{repo}" if self.project else repo


def get_upstream_registry_config() -> UpstreamRegistryConfig:
    return UpstreamRegistryConfig(
        url=os.environ["PROPS_REGISTRY_UPSTREAM_URL"],
        username=os.environ.get("PROPS_REGISTRY_UPSTREAM_USERNAME") or None,
        password=os.environ.get("PROPS_REGISTRY_UPSTREAM_PASSWORD") or None,
        project=os.environ.get("PROPS_REGISTRY_UPSTREAM_PROJECT") or None,
    )


def is_digest(ref: str) -> bool:
    """Check if a reference is a digest (sha256:...) vs a tag."""
    return bool(re.match(r"^(sha256|sha384|sha512):[a-f0-9]+$", ref))
