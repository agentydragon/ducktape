"""HTTP download client with explicit proxy/TLS configuration.

All HTTP downloads in session hooks go through this module so that
proxy and CA settings are passed explicitly from the caller's environment,
not read from the daemon's os.environ.
"""

import logging
import ssl
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpConfig:
    """Explicit HTTP client configuration derived from caller's environment."""

    proxy_url: str | None = None
    ca_file: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "HttpConfig":
        """Construct from an environment dict."""
        return cls(
            proxy_url=env.get("HTTPS_PROXY") or env.get("https_proxy"),
            ca_file=env.get("SSL_CERT_FILE") or env.get("REQUESTS_CA_BUNDLE") or env.get("CURL_CA_BUNDLE"),
        )


def _build_opener(config: HttpConfig) -> urllib.request.OpenerDirector:
    """Build a urllib opener from explicit config."""
    handlers: list[urllib.request.BaseHandler] = []
    if config.proxy_url:
        handlers.append(urllib.request.ProxyHandler({"https": config.proxy_url, "http": config.proxy_url}))
    if config.ca_file:
        ctx = ssl.create_default_context(cafile=config.ca_file)
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def download(url: str, config: HttpConfig, *, timeout: int = 60) -> bytes:
    """Download URL content using explicit proxy/TLS config."""
    opener = _build_opener(config)
    with opener.open(url, timeout=timeout) as response:
        return response.read()
