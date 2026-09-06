import json
import logging
from enum import StrEnum

from aiohttp import web
from mitmproxy import http
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest

CLIENT_METADATA_KEY = "github_proxy_client"
UNAUTHENTICATED = "__unauthenticated__"
MAX_COST_BODY_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)


class CaptureChannel(StrEnum):
    RAW = "raw"
    SESSION_WS = "session_ws"


class Route(StrEnum):
    CONNECT = "connect"
    GRAPHQL = "graphql"
    GITHUB_API = "github_api"
    CLOUD_BATCH = "cloud_batch_branch_status"
    CLOUD_COMPARE = "cloud_compare_refs"
    CLOUD_INSTALLATIONS = "cloud_installations"
    OTHER = "other"


def route(flow: http.HTTPFlow) -> Route:
    if flow.request.method == "CONNECT":
        return Route.CONNECT
    path = flow.request.path.split("?", 1)[0]
    if flow.request.host == "api.github.com":
        return Route.GRAPHQL if path == "/graphql" else Route.GITHUB_API
    if flow.request.host == "claude.ai":
        match path:
            case "/v1/code/github/batch-branch-status":
                return Route.CLOUD_BATCH
            case "/v1/code/github/compare-refs":
                return Route.CLOUD_COMPARE
            case "/v1/code/github/org-connection/installations-status":
                return Route.CLOUD_INSTALLATIONS
    return Route.OTHER


class CostStatus(StrEnum):
    OBSERVED = "observed"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    OVERSIZED = "oversized"
    INVALID = "invalid"


def observed_cost(response: http.Response | None) -> tuple[CostStatus, int | None]:
    if response is None or response.raw_content is None:
        return CostStatus.UNAVAILABLE, None
    # raw_content bounds decoding too: compressed content is not decompressed for metrics.
    if len(response.raw_content) > MAX_COST_BODY_BYTES:
        return CostStatus.OVERSIZED, None
    if response.headers.get("content-encoding", "identity") != "identity":
        return CostStatus.UNAVAILABLE, None
    try:
        payload = json.loads(response.raw_content)
    except (ValueError, RecursionError):
        return CostStatus.INVALID, None
    if not isinstance(payload, dict) or not isinstance(data := payload.get("data"), dict):
        return CostStatus.ABSENT, None
    if not isinstance(rate_limit := data.get("rateLimit"), dict) or "cost" not in rate_limit:
        return CostStatus.ABSENT, None
    cost = rate_limit["cost"]
    if type(cost) is not int or not 0 <= cost <= 2**53:
        return CostStatus.INVALID, None
    return CostStatus.OBSERVED, cost


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.ready = False
        self.failed_captures: set[CaptureChannel] = set()
        self.capture_failures = Counter(
            "github_api_proxy_capture_write_failures",
            "Failed private capture append operations.",
            ("channel",),
            registry=self.registry,
        )
        self.requests = Counter(
            "github_api_proxy_requests",
            "Completed proxied requests, including local responses.",
            ("client", "route", "status"),
            registry=self.registry,
        )
        self.auth = Counter(
            "github_api_proxy_auth",
            "Authentication decisions; unknown client names are never labels.",
            ("client", "stage", "result"),
            registry=self.registry,
        )
        self.cost = Counter(
            "github_api_proxy_graphql_observed_cost",
            "Explicit data.rateLimit.cost only; never header differences.",
            ("client",),
            registry=self.registry,
        )
        self.cost_observations = Counter(
            "github_api_proxy_graphql_cost_observations",
            "Coverage of explicit GraphQL cost observations.",
            ("client", "result"),
            registry=self.registry,
        )

    def running(self) -> None:
        self.ready = True

    def capture_write_failed(self, channel: CaptureChannel, count: int = 1) -> None:
        self.capture_failures.labels(channel).inc(count)
        if channel not in self.failed_captures:
            logger.error("Private capture write failed; readiness disabled (channel=%s)", channel)
        self.failed_captures.add(channel)

    def done(self) -> None:
        self.ready = False

    @property
    def healthy(self) -> bool:
        return self.ready and not self.failed_captures

    def response(self, flow: http.HTTPFlow) -> None:
        client = flow.metadata.get(CLIENT_METADATA_KEY, UNAUTHENTICATED)
        status = (
            str(flow.response.status_code)
            if flow.response is not None and 100 <= flow.response.status_code <= 599
            else "transport_error"
        )
        selected_route = route(flow)
        self.requests.labels(client, selected_route, status).inc()
        if selected_route is Route.GRAPHQL:
            cost_status, cost = observed_cost(flow.response)
            self.cost_observations.labels(client, cost_status).inc()
            if cost is not None:
                self.cost.labels(client).inc(cost)

    def error(self, flow: http.HTTPFlow) -> None:
        self.response(flow)

    def http_connected(self, flow: http.HTTPFlow) -> None:
        self.response(flow)

    def http_connect_error(self, flow: http.HTTPFlow) -> None:
        self.response(flow)

    async def scrape(self, request: web.Request) -> web.Response:
        return web.Response(body=generate_latest(self.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def health(self, request: web.Request) -> web.Response:
        ready = self.healthy
        return web.Response(status=200 if ready else 503, text="ready\n" if ready else "not ready\n")

    def application(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/metrics", self.scrape)
        app.router.add_get("/healthz", self.health)
        return app
