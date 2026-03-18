"""HTTP download client with explicit proxy/TLS configuration.

All HTTP downloads in session hooks go through this module so that
proxy and CA settings are passed explicitly from the caller's environment,
not read from the daemon's os.environ.
"""

import logging
import ssl

import httpx

logger = logging.getLogger(__name__)


def build_http_client(env: dict[str, str], *, timeout: int = 60) -> httpx.Client:
    """Build an httpx client from a caller's environment dict.

    Reads proxy and CA settings from the env and returns a configured client.
    The caller is responsible for closing the client (use as context manager).
    """
    proxy_url = env.get("HTTPS_PROXY") or env.get("https_proxy")
    ca_file = env.get("SSL_CERT_FILE") or env.get("REQUESTS_CA_BUNDLE") or env.get("CURL_CA_BUNDLE")

    ssl_context: ssl.SSLContext | bool = True
    if ca_file:
        ssl_context = ssl.create_default_context(cafile=ca_file)

    return httpx.Client(proxy=proxy_url, verify=ssl_context, timeout=timeout, follow_redirects=True)


def download(url: str, client: httpx.Client) -> bytes:
    """Download URL content using a pre-configured httpx client."""
    response = client.get(url)
    response.raise_for_status()
    return response.content
