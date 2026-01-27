"""Exception classes for bazel proxy infrastructure."""


class BazelProxyError(Exception):
    """Base exception for bazel proxy errors."""


class SupervisorError(BazelProxyError):
    """Failed to start/communicate with supervisor."""


class ProxyServiceError(BazelProxyError):
    """Failed to start/restart proxy service."""


class CaExtractionError(BazelProxyError):
    """Failed to extract TLS inspection CA certificate."""


class TruststoreError(BazelProxyError):
    """Failed to create Java truststore."""


class CaBundleError(BazelProxyError):
    """Failed to create combined CA bundle."""


class MissingEnvVarError(BazelProxyError):
    """Required environment variable not set."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} not set. Run: python3 -m claude_hooks.session_start")
        self.name = name


class SkipError(Exception):
    """Component was skipped via environment variable."""

    def __init__(self, component: str) -> None:
        super().__init__(f"{component} setup skipped")
        self.component = component


class DirenvError(Exception):
    """Error running direnv."""


class ProjectNotFoundError(Exception):
    """Could not detect project root directory."""
