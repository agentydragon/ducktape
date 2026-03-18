"""HTTP download client with explicit proxy/TLS configuration.

All HTTP downloads in session hooks go through this module so that
proxy and CA settings are passed explicitly from the caller's environment,
not read from the daemon's os.environ.
"""

import logging
import ssl
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpConfig:
    """Explicit HTTP client configuration derived from caller's environment."""

    proxy_url: str | None
    ca_file: str | None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "HttpConfig":
        """Construct from an environment dict."""
        return cls(
            proxy_url=env.get("HTTPS_PROXY") or env.get("https_proxy"),
            ca_file=env.get("SSL_CERT_FILE") or env.get("REQUESTS_CA_BUNDLE") or env.get("CURL_CA_BUNDLE"),
        )


def _build_client(config: HttpConfig, *, timeout: int = 60) -> httpx.Client:
    """Build an httpx client from explicit config."""
    ssl_context: ssl.SSLContext | bool = True
    if config.ca_file:
        ssl_context = ssl.create_default_context(cafile=config.ca_file)
    return httpx.Client(proxy=config.proxy_url, verify=ssl_context, timeout=timeout, follow_redirects=True)


def download(url: str, config: HttpConfig, *, timeout: int = 60) -> bytes:
    """Download URL content using explicit proxy/TLS config."""
    with _build_client(config, timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content
