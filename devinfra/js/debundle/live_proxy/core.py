from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
from argparse import ArgumentParser
from dataclasses import dataclass, field
from enum import StrEnum
from html import escape as html_escape
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import unquote, urlsplit, urlunsplit

from devinfra.js.debundle.live_proxy.vendor_runtime import (
    PartialSwapEntry,
    VendorRuntimeEntry,
    build_partial_swap_import_map,
    load_partial_swap_runtime_index,
    load_vendor_runtime_index,
    resolve_partial_swap_runtime_request,
    resolve_vendor_runtime_request,
)

MODULE_SCRIPT_RE = re.compile(
    r"<script\b(?=[^>]*\btype\s*=\s*[\"']module[\"'])(?=[^>]*\bsrc\s*=\s*[\"'][^\"']+[\"'])[^>]*>\s*</script>",
    re.IGNORECASE,
)
MODULE_PRELOAD_RE = re.compile(
    r"<link\b(?=[^>]*\brel\s*=\s*[\"'][^\"']*\bmodulepreload\b[^\"']*[\"'])(?=[^>]*\bhref\s*=\s*[\"'][^\"']+[\"'])[^>]*>",
    re.IGNORECASE,
)
SNAPSHOT_ASSET_PREFIXES = ["/static/", "/preload/"]
SNAPSHOT_ASSET_RE = re.compile(
    r"(\b(?:href|src)\s*=\s*[\"'])((?:"
    + "|".join(re.escape(prefix) for prefix in SNAPSHOT_ASSET_PREFIXES)
    + r")[^\"'?#]*)([\"'?#])",
    re.IGNORECASE,
)

DEFAULT_APP_MANIFEST = None
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 8866


@dataclass
class LiveProxyOptions:
    app_manifest_path: Path | None = DEFAULT_APP_MANIFEST
    proxy_host: str = DEFAULT_PROXY_HOST
    proxy_port: int = DEFAULT_PROXY_PORT
    internal_prefix: str | None = None
    package_roots: dict[str, Path] = field(default_factory=dict)
    packages_root: Path | None = None
    state_dir: Path | None = None
    help: bool = False

    def to_json_dict(self) -> dict:
        return {
            "app_manifest_path": str(self.app_manifest_path) if self.app_manifest_path else None,
            "proxy_host": self.proxy_host,
            "proxy_port": self.proxy_port,
            "internal_prefix": self.internal_prefix,
            "package_roots": {name: str(path) for name, path in self.package_roots.items()},
            "packages_root": str(self.packages_root) if self.packages_root else None,
            "state_dir": str(self.state_dir) if self.state_dir else None,
        }

    @classmethod
    def from_json_dict(cls, value: dict) -> LiveProxyOptions:
        return cls(
            app_manifest_path=Path(value["app_manifest_path"]) if value.get("app_manifest_path") else None,
            proxy_host=value.get("proxy_host") or DEFAULT_PROXY_HOST,
            proxy_port=int(value.get("proxy_port") or DEFAULT_PROXY_PORT),
            internal_prefix=value.get("internal_prefix"),
            package_roots={name: Path(path) for name, path in (value.get("package_roots") or {}).items()},
            packages_root=Path(value["packages_root"]) if value.get("packages_root") else None,
            state_dir=Path(value["state_dir"]) if value.get("state_dir") else None,
        )


@dataclass(frozen=True)
class ControlPaths:
    """Same-origin internal URLs the proxy serves directly."""

    live_index: str
    service_worker: str


@dataclass(frozen=True)
class LiveProxyConfig:
    app_asset_prefix: str
    app_manifest_path: Path
    app_root: Path
    asset_summary_path: Path | None
    bootstrap_url: str
    ca_dir: Path
    control_paths: ControlPaths
    injected_html: str
    internal_prefix: str
    out_root: Path
    profile_dir: Path
    proxy_host: str
    proxy_port: int
    source_html_path: Path | None
    state_dir: Path
    target_host: str
    target_origin: str
    target_url: str
    ui_version: str
    vendor_manifest_path: Path
    vendor_runtime_index: dict[str, VendorRuntimeEntry]
    partial_swap_manifest_path: Path
    partial_swap_runtime_index: dict[str, PartialSwapEntry]


class LocalAssetKind(StrEnum):
    LIVE_INDEX = "live-index"
    SERVICE_WORKER = "service-worker"
    VENDOR_FILE = "vendor-file"
    PARTIAL_SWAP_REDIRECT = "partial-swap-redirect"
    PARTIAL_SWAP_FILE = "partial-swap-file"
    FILE = "file"


@dataclass(frozen=True)
class LocalAssetMapping:
    kind: LocalAssetKind
    content_type: str | None = None
    file_path: Path | None = None
    body: bytes | None = None
    redirect_to: str | None = None
    package: str | None = None
    chunk_id: str | None = None


def parse_live_proxy_args(argv: list[str]) -> LiveProxyOptions:
    parser = NonExitingArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--app-manifest", type=Path)
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument("--internal-prefix")
    parser.add_argument("--package-root", action="append", default=[])
    parser.add_argument("--packages-root", type=Path)
    parser.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    parser.add_argument("--proxy-port", default=DEFAULT_PROXY_PORT, type=port_arg)
    parser.add_argument("--state-dir", type=Path)
    namespace, unknown = parser.parse_known_args(argv)
    if unknown:
        raise RuntimeError(f"Unknown argument: {unknown[0]}")

    package_roots: dict[str, Path] = {}
    for value in namespace.package_root:
        package_name, package_root = parse_package_root_arg(value, "--package-root")
        package_roots[package_name] = resolve_path(package_root)

    options = LiveProxyOptions(
        app_manifest_path=resolve_workspace_path(namespace.app_manifest) if namespace.app_manifest else None,
        proxy_host=namespace.proxy_host,
        proxy_port=namespace.proxy_port,
        internal_prefix=namespace.internal_prefix,
        package_roots=package_roots,
        packages_root=resolve_path(namespace.packages_root) if namespace.packages_root else None,
        state_dir=resolve_path(namespace.state_dir) if namespace.state_dir else None,
        help=namespace.help,
    )
    if not options.help:
        if not options.app_manifest_path:
            raise RuntimeError("--app-manifest is required")
        options.app_manifest_path = resolve_path(options.app_manifest_path)
        options.state_dir = resolve_path(options.state_dir or default_state_dir())
    return options


def format_live_proxy_help(program: str = "bazel run //devinfra/js/debundle/live_proxy:serve_bin") -> str:
    return "\n".join(
        [
            f"Usage: {program} -- [options]",
            "",
            "Options:",
            "  --app-manifest <path>   App manifest to mount",
            f"  --proxy-host <host>     MITM proxy listen host (default: {DEFAULT_PROXY_HOST})",
            f"  --proxy-port <port>     MITM proxy listen port (default: {DEFAULT_PROXY_PORT})",
            "  --internal-prefix <p>   Internal same-origin prefix used for local JS assets",
            "  --package-root <p>=<d>  Explicit package dir for swapped vendor chunks (repeatable)",
            "  --packages-root <path>  Package tree root for swapped vendor chunks",
            "  --state-dir <path>      Cache/certificate directory",
            "  --help                  Show this message",
            "",
            "Manual browser flow:",
            "  1. Start the proxy.",
            f"  2. Launch a dedicated Chrome/Chromium profile with --proxy-server=http://{DEFAULT_PROXY_HOST}:{DEFAULT_PROXY_PORT}",
            "     and, for a quick local smoke test, --ignore-certificate-errors.",
            "  3. Open the printed target URL and sign in normally.",
        ]
    )


def load_live_proxy_configuration(raw_options: LiveProxyOptions | dict) -> LiveProxyConfig:
    options = raw_options if isinstance(raw_options, LiveProxyOptions) else LiveProxyOptions.from_json_dict(raw_options)
    if not options.app_manifest_path:
        raise RuntimeError("--app-manifest is required")
    app_manifest_path = resolve_path(options.app_manifest_path)
    package_roots = {name: resolve_path(path) for name, path in options.package_roots.items()}
    packages_root = resolve_path(options.packages_root) if options.packages_root else None
    state_dir = resolve_path(options.state_dir or default_state_dir())

    runtime_report = read_json(app_manifest_path)
    reports_root = app_manifest_path.parent
    source_assets_report = read_optional_json(reports_root / "source_assets.json", {})
    provenance_report = read_optional_json(reports_root / "provenance.json", {})
    asset_summary = source_assets_report.get("asset_summary") or {}
    source_html_path = (
        resolve_relative(reports_root, provenance_report["source_html_path"])
        if provenance_report.get("source_html_path")
        else None
    )
    if "source_html" in provenance_report:
        source_html = provenance_report["source_html"]
    elif source_html_path and source_html_path.exists():
        source_html = source_html_path.read_text(encoding="utf-8")
    else:
        source_html = ""

    target_url = normalize_target_url(
        resolve_app_base_url(runtime_report=runtime_report, asset_summary=asset_summary, reports_root=reports_root)
        or "https://example.test"
    )
    target_parts = urlsplit(target_url)
    ui_version = runtime_report.get("ui_version") or asset_summary.get("uiVersion") or "unknown"
    internal_prefix = normalize_internal_prefix(
        options.internal_prefix or f"{target_parts.path.rstrip('/')}/_debundle/live/{ui_version}"
    )
    app_root = resolve_relative(reports_root, runtime_report.get("app_root") or "../app")
    app_asset_prefix = f"{internal_prefix}/app"
    vendor_manifest_path = reports_root / "vendor_swaps.json"
    vendor_runtime_index = load_vendor_runtime_index(
        manifest_path=vendor_manifest_path, package_roots=package_roots, packages_root=packages_root
    )
    partial_swap_runtime_index = load_partial_swap_runtime_index(
        manifest_path=vendor_manifest_path, package_roots=package_roots, packages_root=packages_root
    )
    bootstrap_path = app_root / "bootstrap.js"
    if not bootstrap_path.exists():
        raise RuntimeError(f"Expected bootstrap.js at {bootstrap_path}")

    import_map = (
        build_partial_swap_import_map(partial_swap_runtime_index, app_asset_prefix)
        if partial_swap_runtime_index
        else None
    )
    target_origin = f"{target_parts.scheme}://{target_parts.netloc}"
    return LiveProxyConfig(
        app_asset_prefix=app_asset_prefix,
        app_manifest_path=app_manifest_path,
        app_root=app_root,
        asset_summary_path=resolve_relative(reports_root, source_assets_report["source_path"])
        if source_assets_report.get("source_path")
        else None,
        bootstrap_url=f"{app_asset_prefix}/bootstrap.js",
        ca_dir=state_dir / "mitm-ca",
        control_paths=ControlPaths(
            live_index=f"{internal_prefix}/live-index.html", service_worker=f"{internal_prefix}/sw.js"
        ),
        injected_html=rewrite_html_for_live_proxy(
            source_html,
            app_asset_prefix=app_asset_prefix,
            bootstrap_url=f"{app_asset_prefix}/bootstrap.js",
            ui_version=ui_version,
            import_map=import_map,
        ),
        internal_prefix=internal_prefix,
        out_root=app_root,
        profile_dir=state_dir / "browser-profile",
        proxy_host=options.proxy_host or DEFAULT_PROXY_HOST,
        proxy_port=options.proxy_port or DEFAULT_PROXY_PORT,
        source_html_path=source_html_path,
        state_dir=state_dir,
        target_host=target_parts.netloc,
        target_origin=target_origin,
        target_url=target_url,
        ui_version=ui_version,
        vendor_manifest_path=vendor_manifest_path,
        vendor_runtime_index=vendor_runtime_index,
        partial_swap_manifest_path=vendor_manifest_path,
        partial_swap_runtime_index=partial_swap_runtime_index,
    )


def read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def read_optional_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return fallback
    return read_json(path)


def resolve_app_base_url(
    *, runtime_report: dict[str, Any], asset_summary: dict[str, Any], reports_root: Path
) -> str | None:
    if asset_summary.get("baseUrl"):
        return cast(str, asset_summary["baseUrl"])
    if runtime_report.get("baseUrl"):
        return cast(str, runtime_report["baseUrl"])
    source_metadata_path = (reports_root / "../app/SOURCE.json").resolve()
    if not source_metadata_path.exists():
        return None
    source_metadata = read_json(source_metadata_path)
    return cast(str | None, source_metadata.get("baseUrl"))


def normalize_target_url(value: str) -> str:
    parts = urlsplit(value)
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def rewrite_html_for_live_proxy(
    source_html: str,
    *,
    app_asset_prefix: str | None = None,
    bootstrap_url: str,
    ui_version: str,
    import_map: dict | None = None,
) -> str:
    html = MODULE_SCRIPT_RE.sub("", source_html)
    html = MODULE_PRELOAD_RE.sub("", html)
    if app_asset_prefix:
        html = rewrite_snapshot_asset_urls(html, app_asset_prefix)

    import_map_script = (
        f'<script type="importmap">{json.dumps(import_map, separators=(",", ":"))}</script>\n    ' if import_map else ""
    )
    injected = (
        f"{import_map_script}{live_proxy_prelude_script(bootstrap_url=bootstrap_url, ui_version=ui_version)}\n"
        f'    <script type="module" crossorigin src="{escape_html_attr(bootstrap_url)}"></script>'
    )
    if re.search(r"</body>", html, flags=re.IGNORECASE):
        html = re.sub(r"</body>", f"    {injected}\n  </body>", html, count=1, flags=re.IGNORECASE)
    else:
        html = f"{html}\n{injected}\n"

    comment = "Generated by //devinfra/js/debundle/live_proxy:serve_bin."
    if comment not in html:
        html = re.sub(r"<head>", f"<head>\n    <!-- {comment} -->", html, count=1, flags=re.IGNORECASE)
    return html if html.endswith("\n") else f"{html}\n"


def rewrite_snapshot_asset_urls(html: str, app_asset_prefix: str) -> str:
    return SNAPSHOT_ASSET_RE.sub(
        lambda match: f"{match.group(1)}{app_asset_prefix}{match.group(2)}{match.group(3)}", html
    )


def is_target_document_request(method: str, headers: dict, config: LiveProxyConfig) -> bool:
    host = normalize_host(header_get(headers, "host") or "")
    if host != normalize_host(config.target_host):
        return False
    destination = (header_get(headers, "sec-fetch-dest") or "").lower()
    if destination in {"document", "iframe"}:
        return method == "GET"
    accept = (header_get(headers, "accept") or "").lower()
    return method == "GET" and "text/html" in accept


def map_local_asset_path(pathname: str, config: LiveProxyConfig) -> LocalAssetMapping | None:
    normalized_path = pathname.split("?", 1)[0]
    if not normalized_path.startswith(f"{config.internal_prefix}/"):
        return None
    if normalized_path == config.control_paths.live_index:
        return LocalAssetMapping(
            kind=LocalAssetKind.LIVE_INDEX,
            body=config.injected_html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )
    if normalized_path == config.control_paths.service_worker:
        return LocalAssetMapping(
            kind=LocalAssetKind.SERVICE_WORKER,
            body=noop_service_worker_source().encode("utf-8"),
            content_type="text/javascript; charset=utf-8",
        )

    suffix = unquote(normalized_path[len(config.internal_prefix) + 1 :])
    vendor_runtime = resolve_vendor_runtime_request(suffix, config.vendor_runtime_index)
    if vendor_runtime:
        return LocalAssetMapping(
            kind=LocalAssetKind.VENDOR_FILE,
            chunk_id=vendor_runtime.entry.chunk_id,
            content_type=content_type_for_path(vendor_runtime.file_path),
            file_path=vendor_runtime.file_path,
        )
    partial_swap = resolve_partial_swap_runtime_request(suffix, config.partial_swap_runtime_index)
    if partial_swap:
        if partial_swap.resolved_suffix and partial_swap.resolved_suffix != partial_swap.request_suffix:
            return LocalAssetMapping(
                kind=LocalAssetKind.PARTIAL_SWAP_REDIRECT,
                redirect_to=f"{config.internal_prefix}/app/_partial_swap/{partial_swap.entry.package}/{partial_swap.resolved_suffix}",
            )
        return LocalAssetMapping(
            kind=LocalAssetKind.PARTIAL_SWAP_FILE,
            content_type=content_type_for_path(partial_swap.file_path),
            file_path=partial_swap.file_path,
            package=partial_swap.entry.package,
        )

    app_relative_path = strip_app_asset_prefix(suffix)
    if app_relative_path is None:
        return None
    return LocalAssetMapping(
        kind=LocalAssetKind.FILE,
        content_type=content_type_for_path(Path(app_relative_path)),
        file_path=safe_join(config.app_root or config.out_root, app_relative_path),
    )


def map_snapshot_asset_path(pathname: str, config: LiveProxyConfig) -> LocalAssetMapping | None:
    """Map an original root-absolute snapshot URL onto the emitted app tree.

    Runtime-created workers can retain URLs such as ``/static/worker-X.js``.
    Unlike HTML assets, those URLs never pass through the live proxy's HTML
    rewrite.  JavaScript chunks are materialized as ``<chunk>/entry.js`` while
    non-JavaScript snapshot assets keep their original path.

    Only claim the request when the corresponding local file exists (or the
    chunk is a configured vendor swap), so unrelated target-origin endpoints
    continue upstream unchanged.
    """
    normalized_path = pathname.split("?", 1)[0]
    if not any(normalized_path.startswith(prefix) for prefix in SNAPSHOT_ASSET_PREFIXES):
        return None

    snapshot_relative_path = unquote(normalized_path).lstrip("/")
    if snapshot_relative_path.endswith(".js"):
        chunk_id = snapshot_relative_path[: -len(".js")]
        vendor_runtime = config.vendor_runtime_index.get(chunk_id)
        if vendor_runtime:
            return LocalAssetMapping(
                kind=LocalAssetKind.VENDOR_FILE,
                chunk_id=vendor_runtime.chunk_id,
                content_type=content_type_for_path(vendor_runtime.file_path),
                file_path=vendor_runtime.file_path,
            )
        app_relative_path = f"{chunk_id}/entry.js"
    else:
        app_relative_path = snapshot_relative_path

    file_path = safe_join(config.app_root or config.out_root, app_relative_path)
    if not file_path.is_file():
        return None
    return LocalAssetMapping(
        kind=LocalAssetKind.FILE, content_type=content_type_for_path(file_path), file_path=file_path
    )


def strip_app_asset_prefix(relative_path: str) -> str | None:
    normalized = normalize_relative_path(relative_path)
    if normalized == "app":
        return ""
    if not normalized.startswith("app/"):
        return None
    return normalized[len("app/") :]


def normalize_relative_path(value: str) -> str:
    return "/".join(segment for segment in re.split(r"[\\/]+", value) if segment)


def live_proxy_prelude_script(*, bootstrap_url: str, ui_version: str) -> str:
    registration_literal = json.dumps(
        {"active": None, "installing": None, "scope": "/", "waiting": None}, separators=(",", ":")
    )
    return f"""<script>
      (() => {{
        const tag = "[js-debundle-live-proxy]";
        const noopRegistration = {{
          ...{registration_literal},
          addEventListener() {{}},
          async unregister() {{ return true; }},
          async update() {{}},
        }};
        const serviceWorkerStub = {{
          controller: null,
          ready: Promise.resolve(noopRegistration),
          addEventListener() {{}},
          removeEventListener() {{}},
          register: async () => noopRegistration,
          getRegistration: async () => undefined,
          getRegistrations: async () => [],
          startMessages() {{}},
        }};
        globalThis.__jsDebundleLiveProxy = {{
          active: true,
          bootstrapUrl: {json.dumps(bootstrap_url)},
          uiVersion: {json.dumps(ui_version)},
        }};
        document.documentElement.dataset.jsDebundleLiveProxy = "true";
        const existing = navigator.serviceWorker;
        if (existing && typeof existing.getRegistrations === "function") {{
          existing.getRegistrations().then((registrations) => {{
            for (const registration of registrations) {{
              registration.unregister().catch(() => {{}});
            }}
          }}).catch(() => {{}});
        }}
        try {{
          Object.defineProperty(navigator, "serviceWorker", {{
            configurable: true,
            value: serviceWorkerStub,
          }});
        }} catch (error) {{
          if (existing) {{
            try {{ existing.register = serviceWorkerStub.register; }} catch {{}}
            try {{ existing.getRegistration = serviceWorkerStub.getRegistration; }} catch {{}}
            try {{ existing.getRegistrations = serviceWorkerStub.getRegistrations; }} catch {{}}
            try {{ existing.ready = serviceWorkerStub.ready; }} catch {{}}
          }}
          console.warn(tag, "unable to replace navigator.serviceWorker directly", error);
        }}
        console.info(tag, "active", globalThis.__jsDebundleLiveProxy);
      }})();
    </script>"""


def noop_service_worker_source() -> str:
    return "\n".join(
        [
            "self.addEventListener('install', (event) => {",
            "  self.skipWaiting();",
            "});",
            "self.addEventListener('activate', (event) => {",
            "  event.waitUntil(self.clients.claim());",
            "});",
            "self.addEventListener('fetch', () => {});",
            "",
        ]
    )


def safe_join(root: Path, relative_path: str) -> Path:
    normalized = posixpath.normpath(relative_path).lstrip("/\\")
    resolved_path = (root / normalized).resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise RuntimeError(f"Refusing to serve path outside root: {relative_path}") from exc
    return resolved_path


def content_type_for_path(path: Path) -> str:
    if path.suffix in {".js", ".mjs"}:
        return "text/javascript; charset=utf-8"
    if path.suffix == ".map":
        return "application/json; charset=utf-8"
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed is None:
        return "application/octet-stream"
    if guessed.startswith("text/") or guessed == "application/json":
        return f"{guessed}; charset=utf-8"
    return guessed


def normalize_internal_prefix(prefix: str) -> str:
    normalized = prefix.rstrip("/")
    if not normalized.startswith("/"):
        raise RuntimeError(f"Internal prefix must start with /, got {prefix}")
    return normalized


def normalize_host(host: str) -> str:
    return re.sub(r":\d+$", "", host).lower()


def default_state_dir() -> Path:
    return Path("/tmp") / "js-debundle-live-proxy"


def resolve_path(path: str | Path) -> Path:
    return resolve_workspace_path(path)


def resolve_workspace_path(path: str | Path) -> Path:
    value = Path(path)
    if value.is_absolute():
        return value.resolve()

    candidates = [Path.cwd() / value]
    for env_name in ["BUILD_WORKSPACE_DIRECTORY", "BUILD_WORKING_DIRECTORY", "PWD", "RUNFILES_DIR", "TEST_SRCDIR"]:
        root = os.environ.get(env_name)
        if root:
            candidates.append(Path(root) / value)
            candidates.append(Path(root) / "_main" / value)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def resolve_relative(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def parse_package_root_arg(value: str, flag: str) -> tuple[str, Path]:
    separator = value.find("=")
    if separator <= 0 or separator == len(value) - 1:
        raise RuntimeError(f"{flag} must be in <package>=<dir> form, got {value}")
    return value[:separator], Path(value[separator + 1 :])


def parse_port(value: str, flag: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{flag} must be a valid TCP port, got {value}") from exc
    if parsed < 1 or parsed > 65535:
        raise RuntimeError(f"{flag} must be a valid TCP port, got {value}")
    return parsed


def port_arg(value: str) -> int:
    return parse_port(value, "port")


def header_get(headers: dict, name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return None


def escape_html_attr(value: str) -> str:
    return html_escape(value, quote=True)


class NonExitingArgumentParser(ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise RuntimeError(message)
