from __future__ import annotations

from pathlib import Path

from mitmproxy import http

from devinfra.js.debundle.live_proxy.core import LocalAssetKind, LocalAssetMapping

NO_STORE = "no-store"
TEXT_PLAIN = "text/plain; charset=utf-8"


def response_for_mapping(mapping: LocalAssetMapping) -> http.Response:
    if mapping.kind == LocalAssetKind.PARTIAL_SWAP_REDIRECT:
        return http.Response.make(301, b"", {"location": mapping.redirect_to or "/"})
    if mapping.body is not None:
        return ok_response(mapping.body, mapping.content_type)
    if not mapping.file_path or not Path(mapping.file_path).is_file():
        return text_response(404, f"missing local asset: {mapping.file_path}\n")
    return ok_response(Path(mapping.file_path).read_bytes(), mapping.content_type)


def unknown_asset_response() -> http.Response:
    return text_response(404, "unknown local asset\n")


def ok_response(body: bytes, content_type: str | None) -> http.Response:
    return http.Response.make(
        200, body, {"cache-control": NO_STORE, "content-type": content_type or "application/octet-stream"}
    )


def text_response(status_code: int, body: str) -> http.Response:
    return http.Response.make(
        status_code, body.encode("utf-8"), {"cache-control": NO_STORE, "content-type": TEXT_PLAIN}
    )
