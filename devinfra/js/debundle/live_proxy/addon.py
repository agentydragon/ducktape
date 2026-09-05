from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from mitmproxy import http

from devinfra.js.debundle.live_proxy.core import (
    LiveProxyOptions,
    header_get,
    is_target_document_request,
    load_live_proxy_configuration,
    map_local_asset_path,
    map_snapshot_asset_path,
    normalize_host,
)
from devinfra.js.debundle.live_proxy.responses import response_for_mapping, unknown_asset_response

OPTIONS_ENV = "JS_DEBUNDLE_LIVE_PROXY_OPTIONS"


class DebundleLiveProxyAddon:
    def __init__(self, options: LiveProxyOptions | dict):
        self.config = load_live_proxy_configuration(options)

    @classmethod
    def from_environment(cls) -> DebundleLiveProxyAddon:
        raw = os.environ.get(OPTIONS_ENV)
        if not raw:
            raise RuntimeError(f"{OPTIONS_ENV} is required")
        return cls(LiveProxyOptions.from_json_dict(json.loads(raw)))

    def request(self, flow: http.HTTPFlow) -> None:
        headers = dict(flow.request.headers)
        host = normalize_host(flow.request.host or header_get(headers, "host") or "")
        if host != normalize_host(self.config.target_host):
            return
        headers.setdefault("host", host)

        request_path = urlsplit(flow.request.path or "/").path
        if request_path == "/sw.js":
            self._serve_local_mapping(flow, self.config.control_paths.service_worker)
            return
        if request_path.startswith(f"{self.config.internal_prefix}/"):
            self._serve_local_mapping(flow, flow.request.path)
            return
        snapshot_mapping = map_snapshot_asset_path(request_path, self.config)
        if snapshot_mapping is not None:
            flow.response = response_for_mapping(snapshot_mapping)
            return
        if is_target_document_request(flow.request.method, headers, self.config):
            self._serve_local_mapping(flow, self.config.control_paths.live_index)
            return

    def _serve_local_mapping(self, flow: http.HTTPFlow, path: str) -> None:
        request_path = urlsplit(path or "/").path
        mapping = map_local_asset_path(request_path, self.config)
        if mapping is None:
            flow.response = unknown_asset_response()
            return
        flow.response = response_for_mapping(mapping)


def addon_from_environment() -> DebundleLiveProxyAddon:
    return DebundleLiveProxyAddon.from_environment()
