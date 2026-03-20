"""mitmproxy addon for structured request/response logging in E2E tests."""

import logging

logger = logging.getLogger(__name__)


class RequestLogger:
    """Log every proxied request with method, URL, status, and size."""

    def request(self, flow):
        logger.info("%s %s", flow.request.method, flow.request.pretty_url)

    def response(self, flow):
        size = len(flow.response.content) if flow.response.content else 0
        logger.info(
            "%s %s -> %d (%d bytes)", flow.request.method, flow.request.pretty_url, flow.response.status_code, size
        )


addons = [RequestLogger()]
