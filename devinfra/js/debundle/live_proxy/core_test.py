from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.js.debundle.live_proxy.addon import DebundleLiveProxyAddon
from devinfra.js.debundle.live_proxy.core import (
    LocalAssetKind,
    LocalAssetMapping,
    is_target_document_request,
    load_live_proxy_configuration,
    map_local_asset_path,
    map_snapshot_asset_path,
    parse_live_proxy_args,
    rewrite_html_for_live_proxy,
)


class LiveProxyCoreTest(unittest.TestCase):
    def test_parse_args_resolves_manifest_and_package_roots(self) -> None:
        fixture = write_base_fixture()
        options = parse_live_proxy_args(
            [
                "--app-manifest",
                str(fixture.app_manifest_path),
                "--package-root",
                f"katex={fixture.packages_root / 'katex'}",
                "--proxy-port",
                "9001",
            ]
        )

        assert options.app_manifest_path == fixture.app_manifest_path
        assert options.package_roots == {"katex": fixture.packages_root / "katex"}
        assert options.proxy_port == 9001

    def test_load_config_rewrites_shell(self) -> None:
        fixture = write_base_fixture()
        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9800,
                "state_dir": str(fixture.root / "state"),
            }
        )

        assert config.target_origin == "https://example.test"
        assert config.bootstrap_url == "/_debundle/live/example/app/bootstrap.js"
        assert config.control_paths.live_index == "/_debundle/live/example/live-index.html"
        assert 'src="/_debundle/live/example/app/bootstrap.js"' in config.injected_html
        assert "js-debundle-live-proxy" in config.injected_html

        rewritten = rewrite_html_for_live_proxy(
            fixture.source_html_path.read_text(encoding="utf-8"),
            bootstrap_url="/_debundle/live/example/app/bootstrap.js",
            ui_version="example",
        )
        assert 'src="/_debundle/live/example/app/bootstrap.js"' in rewritten
        assert "/static/index-Example.js" not in rewritten
        assert "/static/vendor-Example.js" not in rewritten
        assert "/static/index.css" in rewritten

    def test_load_config_falls_back_to_source_json(self) -> None:
        fixture = write_base_fixture(source_base_url="https://app.example.com", ui_version="source-fallback")
        write_json(
            fixture.source_assets_report_path, {"source_path": str(fixture.asset_summary_path), "asset_summary": {}}
        )

        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9803,
                "state_dir": str(fixture.root / "state"),
            }
        )

        assert config.target_origin == "https://app.example.com"
        assert config.target_url == "https://app.example.com/"
        assert config.bootstrap_url == "/_debundle/live/source-fallback/app/bootstrap.js"

    def test_manifest_relative_paths_are_resolved_from_reports_dir(self) -> None:
        fixture = write_base_fixture(source_base_url="https://app.example.com", ui_version="runfiles")
        write_json(fixture.asset_summary_path, {})
        write_json(fixture.app_manifest_path, {"app_root": "../app", "ui_version": "runfiles"})
        write_json(fixture.source_assets_report_path, {"source_path": "../asset-summary.json", "asset_summary": {}})
        write_json(
            fixture.provenance_report_path,
            {
                "source_html_path": "../source/index.html",
                "source_html": fixture.source_html_path.read_text(encoding="utf-8"),
            },
        )

        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9804,
                "state_dir": str(fixture.root / "state"),
            }
        )

        assert config.asset_summary_path == fixture.asset_summary_path
        assert config.source_html_path == fixture.source_html_path
        assert config.app_root == fixture.app_root
        assert config.target_origin == "https://app.example.com"

    def test_app_root_can_have_non_app_directory_name(self) -> None:
        fixture = write_base_fixture(app_relative_out_dir="out/v-example", ui_version="versioned")
        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9805,
                "state_dir": str(fixture.root / "state"),
            }
        )

        mapping = require_mapping(map_local_asset_path("/_debundle/live/versioned/app/bootstrap.js", config))
        assert mapping.file_path == fixture.app_root / "bootstrap.js"

    def test_rewrite_retargets_snapshot_assets(self) -> None:
        source_html = "\n".join(
            [
                "<!doctype html>",
                "<html><head>",
                '  <link href="/preload/style.css" rel="stylesheet" />',
                '  <link rel="stylesheet" crossorigin href="/static/index-Example.css">',
                '  <link rel="icon" href="/favicon.ico">',
                '  <script type="module" crossorigin src="/static/index-Example.js"></script>',
                '</head><body><div id="app"></div></body></html>',
            ]
        )
        rewritten = rewrite_html_for_live_proxy(
            source_html,
            app_asset_prefix="/_debundle/live/example/app",
            bootstrap_url="/_debundle/live/example/app/bootstrap.js",
            ui_version="example",
        )

        assert 'href="/_debundle/live/example/app/preload/style.css"' in rewritten
        assert 'href="/_debundle/live/example/app/static/index-Example.css"' in rewritten
        assert 'href="/preload/style.css"' not in rewritten
        assert 'href="/favicon.ico"' in rewritten

    def test_is_target_document_request(self) -> None:
        fixture = write_base_fixture()
        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9807,
                "state_dir": str(fixture.root / "state"),
            }
        )
        assert config.target_host == "example.test"

        assert is_target_document_request(
            "GET",
            {"accept": "text/html,application/xhtml+xml", "host": "example.test", "sec-fetch-dest": "document"},
            config,
        )
        assert not is_target_document_request(
            "GET", {"accept": "application/json", "host": "api.example.test", "sec-fetch-dest": "empty"}, config
        )

    def test_addon_rewrites_document_when_host_header_is_absent(self) -> None:
        fixture = write_base_fixture()
        addon = DebundleLiveProxyAddon(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9806,
                "state_dir": str(fixture.root / "state"),
            }
        )

        flow = FakeFlow(
            host="example.test",
            method="GET",
            path="/",
            headers={"accept": "text/html,application/xhtml+xml", "sec-fetch-dest": "document"},
        )
        addon.request(flow)

        assert flow.response is not None
        assert b"js-debundle-live-proxy" in flow.response.content

    def test_map_local_assets(self) -> None:
        fixture = write_base_fixture()
        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9800,
                "state_dir": str(fixture.root / "state"),
            }
        )

        file_mapping = require_mapping(map_local_asset_path("/_debundle/live/example/app/bootstrap.js", config))
        assert file_mapping.kind == LocalAssetKind.FILE
        assert file_mapping.file_path == fixture.app_root / "bootstrap.js"

        html_mapping = require_mapping(map_local_asset_path("/_debundle/live/example/live-index.html", config))
        assert html_mapping.kind == LocalAssetKind.LIVE_INDEX
        assert b"js-debundle-live-proxy" in (html_mapping.body or b"")

        sw_mapping = require_mapping(map_local_asset_path("/_debundle/live/example/sw.js", config))
        assert sw_mapping.kind == LocalAssetKind.SERVICE_WORKER
        assert b"skipWaiting" in (sw_mapping.body or b"")

    def test_maps_root_absolute_snapshot_chunk_to_materialized_entry(self) -> None:
        fixture = write_base_fixture()
        write_text(fixture.app_root / "static" / "worker-Example" / "entry.js", "self.postMessage('ready');\n")
        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9800,
                "state_dir": str(fixture.root / "state"),
            }
        )

        mapping = require_mapping(map_snapshot_asset_path("/static/worker-Example.js", config))
        assert mapping.kind == LocalAssetKind.FILE
        assert mapping.file_path == fixture.app_root / "static" / "worker-Example" / "entry.js"
        assert mapping.content_type == "text/javascript; charset=utf-8"

    def test_addon_serves_root_absolute_snapshot_chunk(self) -> None:
        fixture = write_base_fixture()
        write_text(fixture.app_root / "static" / "worker-Example" / "entry.js", "self.postMessage('ready');\n")
        addon = DebundleLiveProxyAddon(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9800,
                "state_dir": str(fixture.root / "state"),
            }
        )

        flow = FakeFlow(
            host="example.test",
            method="GET",
            path="/static/worker-Example.js",
            headers={"accept": "*/*", "sec-fetch-dest": "script"},
        )
        addon.request(flow)

        assert flow.response is not None
        assert flow.response.status_code == 200
        assert flow.response.content == b"self.postMessage('ready');\n"
        assert flow.response.headers["content-type"] == "text/javascript; charset=utf-8"

    def test_addon_leaves_unknown_snapshot_asset_for_upstream(self) -> None:
        fixture = write_base_fixture()
        addon = DebundleLiveProxyAddon(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9800,
                "state_dir": str(fixture.root / "state"),
            }
        )

        flow = FakeFlow(
            host="example.test",
            method="GET",
            path="/static/not-in-snapshot.js",
            headers={"accept": "*/*", "sec-fetch-dest": "script"},
        )
        addon.request(flow)

        assert flow.response is None

    def test_vendor_and_partial_swap_resolution(self) -> None:
        fixture = write_base_fixture(ui_version="vendor")
        write_text(
            fixture.packages_root / "katex" / "dist" / "katex.mjs",
            'export { helper } from "./helpers/helper.mjs";\nexport const render = () => "katex";\n',
        )
        write_text(fixture.packages_root / "katex" / "dist" / "helpers" / "helper.mjs", "export const helper = 1;\n")
        write_json(fixture.packages_root / "katex" / "package.json", {"name": "katex", "version": "0.16.19"})
        write_text(fixture.packages_root / "mobx-react-lite" / "dist" / "index.js", "export * from './platform';\n")
        write_text(
            fixture.packages_root / "mobx-react-lite" / "dist" / "platform" / "index.js", "export const p = 1;\n"
        )
        write_json(
            fixture.packages_root / "mobx-react-lite" / "package.json", {"name": "mobx-react-lite", "version": "4.1.1"}
        )
        write_text(
            fixture.vendors_root / "generated" / "static" / "native-B5Vb9Oiz" / "runtime.js",
            "export const native = true;\n",
        )
        write_json(
            fixture.reports_root / "vendor_swaps.json",
            {
                "full": {
                    "static/katex-BZy9Y_85.js": {
                        "chunk_path": "static/katex-BZy9Y_85.js",
                        "entry_file": "runtime.js",
                        "package": "katex",
                        "version": "0.16.19",
                        "subpath": "dist/katex.mjs",
                    },
                    "static/native-B5Vb9Oiz.js": {
                        "chunk_path": "static/native-B5Vb9Oiz.js",
                        "entry_file": "runtime.js",
                        "package": "@emoji-mart/data",
                        "version": "1.2.1",
                        "subpath": "sets/15/native.json",
                        "generated_wrapper_path": "../app/vendors/generated/static/native-B5Vb9Oiz/runtime.js",
                    },
                },
                "partial": {
                    "static/react-family.js": {
                        "packages": {"mobx-react-lite": {"version": "4.1.1", "subpath": "dist/index.js"}}
                    }
                },
            },
        )

        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(fixture.app_manifest_path),
                "packages_root": str(fixture.packages_root),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9800,
                "state_dir": str(fixture.root / "state"),
            }
        )

        runtime_hit = require_mapping(
            map_local_asset_path("/_debundle/live/vendor/app/static/katex-BZy9Y_85/runtime.js", config)
        )
        assert runtime_hit.kind == LocalAssetKind.VENDOR_FILE
        assert runtime_hit.file_path == fixture.packages_root / "katex" / "dist" / "katex.mjs"

        sibling_hit = require_mapping(
            map_local_asset_path("/_debundle/live/vendor/app/static/katex-BZy9Y_85/helpers/helper.mjs", config)
        )
        assert sibling_hit.file_path == fixture.packages_root / "katex" / "dist" / "helpers" / "helper.mjs"

        wrapper_hit = require_mapping(
            map_local_asset_path("/_debundle/live/vendor/app/static/native-B5Vb9Oiz/runtime.js", config)
        )
        assert wrapper_hit.file_path == fixture.vendors_root / "generated" / "static" / "native-B5Vb9Oiz" / "runtime.js"

        partial_redirect = require_mapping(
            map_local_asset_path("/_debundle/live/vendor/app/_partial_swap/mobx-react-lite/dist/platform", config)
        )
        assert partial_redirect.kind == LocalAssetKind.PARTIAL_SWAP_REDIRECT
        assert (
            partial_redirect.redirect_to
            == "/_debundle/live/vendor/app/_partial_swap/mobx-react-lite/dist/platform/index.js"
        )

    def test_missing_vendor_manifest_and_path_escape(self) -> None:
        no_vendor = write_base_fixture(ui_version="novendor")
        load_live_proxy_configuration(
            {
                "app_manifest_path": str(no_vendor.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9801,
                "state_dir": str(no_vendor.root / "state"),
            }
        )

        escape_fixture = write_base_fixture(ui_version="escape")
        config = load_live_proxy_configuration(
            {
                "app_manifest_path": str(escape_fixture.app_manifest_path),
                "proxy_host": "127.0.0.1",
                "proxy_port": 9802,
                "state_dir": str(escape_fixture.root / "state"),
            }
        )
        with pytest.raises(RuntimeError, match="Refusing to serve path outside root"):
            map_local_asset_path("/_debundle/live/escape/app/../../etc/passwd", config)


class Fixture:
    def __init__(
        self,
        *,
        app_manifest_path: Path,
        app_root: Path,
        asset_summary_path: Path,
        packages_root: Path,
        provenance_report_path: Path,
        reports_root: Path,
        root: Path,
        source_assets_report_path: Path,
        source_html_path: Path,
        vendors_root: Path,
    ) -> None:
        self.app_manifest_path = app_manifest_path
        self.app_root = app_root
        self.asset_summary_path = asset_summary_path
        self.packages_root = packages_root
        self.provenance_report_path = provenance_report_path
        self.reports_root = reports_root
        self.root = root
        self.source_assets_report_path = source_assets_report_path
        self.source_html_path = source_html_path
        self.vendors_root = vendors_root


class FakeRequest:
    def __init__(self, *, host: str, method: str, path: str, headers: dict[str, str]) -> None:
        self.host = host
        self.method = method
        self.path = path
        self.headers = headers


class FakeFlow:
    def __init__(self, *, host: str, method: str, path: str, headers: dict[str, str]) -> None:
        self.request = FakeRequest(host=host, method=method, path=path, headers=headers)
        self.response = None


def write_base_fixture(
    *, app_relative_out_dir: str = "app", source_base_url: str | None = None, ui_version: str = "example"
) -> Fixture:
    root = Path(tempfile.mkdtemp(prefix="debundle-live-proxy-"))
    packages_root = root / "node_modules"
    source_root = root / "source"
    app_root = root / app_relative_out_dir
    reports_root = root / "reports"
    vendors_root = app_root / "vendors"
    asset_summary_path = root / "asset-summary.json"
    source_html_path = source_root / "index.html"
    app_manifest_path = reports_root / "runtime.json"
    source_assets_report_path = reports_root / "source_assets.json"
    provenance_report_path = reports_root / "provenance.json"

    write_text(
        source_html_path,
        "\n".join(
            [
                "<!doctype html>",
                "<html>",
                "  <head>",
                '    <link href="/preload/style.css" rel="stylesheet" />',
                '    <script type="module" crossorigin src="/static/index-Example.js"></script>',
                '    <link rel="modulepreload" crossorigin href="/static/vendor-Example.js">',
                '    <link rel="stylesheet" crossorigin href="/static/index.css">',
                "  </head>",
                "  <body>",
                '    <div id="app"></div>',
                "  </body>",
                "</html>",
                "",
            ]
        ),
    )
    write_json(asset_summary_path, {"baseUrl": "https://example.test"})
    write_text(app_root / "bootstrap.js", 'import "./static/index-Example/runtime.js";\n')
    write_text(app_root / "static" / "index-Example" / "runtime.js", "console.log('runtime');\n")
    write_json(
        source_assets_report_path,
        {"source_path": str(asset_summary_path), "asset_summary": {"baseUrl": "https://example.test"}},
    )
    write_json(
        provenance_report_path,
        {"source_html_path": str(source_html_path), "source_html": source_html_path.read_text(encoding="utf-8")},
    )
    write_json(app_manifest_path, {"app_root": str(app_root), "ui_version": ui_version})
    if source_base_url:
        write_json(app_root / "SOURCE.json", {"baseUrl": source_base_url, "uiVersion": ui_version})

    return Fixture(
        app_manifest_path=app_manifest_path,
        app_root=app_root,
        asset_summary_path=asset_summary_path,
        packages_root=packages_root,
        provenance_report_path=provenance_report_path,
        reports_root=reports_root,
        root=root,
        source_assets_report_path=source_assets_report_path,
        source_html_path=source_html_path,
        vendors_root=vendors_root,
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    write_text(path, f"{json.dumps(value, indent=2)}\n")


def require_mapping(mapping: LocalAssetMapping | None) -> LocalAssetMapping:
    if mapping is None:
        raise AssertionError("expected local asset mapping")
    return mapping


if __name__ == "__main__":
    pytest_bazel.main()
