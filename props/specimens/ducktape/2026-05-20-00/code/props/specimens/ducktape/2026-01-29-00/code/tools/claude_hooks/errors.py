"""Exception classes for auth proxy infrastructure."""


class AuthProxyError(Exception):
    """Base exception for auth proxy errors."""


class SupervisorError(AuthProxyError):
    """Failed to start/communicate with supervisor."""


class ProxyServiceError(AuthProxyError):
    """Failed to start/restart proxy service."""


class CaExtractionError(AuthProxyError):
    """Failed to extract TLS inspection CA certificate."""


class TruststoreError(AuthProxyError):
    """Failed to create Java truststore."""


class CaBundleError(AuthProxyError):
    """Failed to create combined CA bundle."""


class MissingEnvVarError(AuthProxyError):
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
