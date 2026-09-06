import hmac
import weakref

from aiohttp import BasicAuth
from mitmproxy import connection, http
from pydantic import SecretStr

from cluster.proxies.github_api_proxy.metrics import CLIENT_METADATA_KEY, UNAUTHENTICATED, Metrics, Route, route


def scrub(flow: http.HTTPFlow) -> None:
    flow.request.headers.pop("Proxy-Authorization", None)
    flow.metadata.pop("proxyauth", None)


class Authenticate:
    def __init__(self, credentials: dict[str, SecretStr], metrics: Metrics, *, block_cloud_github_batch: bool) -> None:
        self.credentials = credentials
        self.metrics = metrics
        self.block_cloud_github_batch = block_cloud_github_batch
        self.tunnels: weakref.WeakKeyDictionary[connection.Client, str] = weakref.WeakKeyDictionary()

    def authenticate(self, flow: http.HTTPFlow, *, connect: bool) -> str | None:
        headers = flow.request.headers.get_all("Proxy-Authorization")
        scrub(flow)
        # No credential-bearing transport is accepted outside the outer TLS connection.
        client = None
        if flow.client_conn.tls:
            if not connect:
                client = self.tunnels.get(flow.client_conn)
            if client is None and len(headers) == 1:
                try:
                    supplied = BasicAuth.decode(headers[0], encoding="utf-8")
                except ValueError:
                    supplied = None
                if (
                    supplied is not None
                    and (expected := self.credentials.get(supplied.login)) is not None
                    and hmac.compare_digest(expected.get_secret_value().encode(), supplied.password.encode())
                ):
                    client = supplied.login
        self.metrics.auth.labels(
            client or UNAUTHENTICATED,
            "connect" if connect else "request",
            "accepted" if client is not None else "denied" if flow.client_conn.tls else "non_tls",
        ).inc()
        if client is None:
            flow.response = http.Response.make(
                407,
                b"Proxy authentication required\n",
                http.Headers(proxy_authenticate='Basic realm="github-api-proxy"', connection="close"),
            )
        else:
            flow.metadata[CLIENT_METADATA_KEY] = client
        return client

    def http_connect(self, flow: http.HTTPFlow) -> None:
        self.authenticate(flow, connect=True)

    def http_connected(self, flow: http.HTTPFlow) -> None:
        # This hook runs only for successful CONNECT, after destination policy.
        # A denied tunnel must not authorize another outer HTTP request.
        self.tunnels[flow.client_conn] = flow.metadata[CLIENT_METADATA_KEY]

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        if self.authenticate(flow, connect=False) is None:
            return
        if flow.request.method == "GET" and flow.request.host == "mitm.it" and flow.request.path == "/":
            flow.response = http.Response.make(
                200 if self.metrics.healthy else 503,
                b"Authenticated proxy ready\n" if self.metrics.healthy else b"Proxy observation unavailable\n",
            )
            return
        if self.block_cloud_github_batch and flow.request.method == "POST" and route(flow) is Route.CLOUD_BATCH:
            flow.response = http.Response.make(
                429, b"Cloud GitHub batch polling temporarily blocked\n", http.Headers(retry_after="3600")
            )
