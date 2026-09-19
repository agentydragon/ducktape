"""End-to-end Grafana dashboard verifier.

This is intentionally a local-execution tool: it talks to the live Grafana API
and launches a real browser. Use ``bb run //cluster/skills/grafana:verify_dashboard``
instead of running it as a remote Bazel action.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from playwright.sync_api import Page, sync_playwright

DEFAULT_GRAFANA_URL = "https://grafana.allegedly.works"
DEFAULT_SECRET = "monitoring/grafana-admin-password"
# TODO: Consider a standalone grafana-image-renderer service for verifier and scheduled
# screenshots. First measure idle RSS and render-time CPU/RAM, then pin the image,
# configure its token/callback, add a NetworkPolicy, and scrape renderer metrics.
# TODO: Replace admin-secret auth with narrower identities: Viewer for deployed-dashboard
# checks and the minimum write-capable scope needed for temporary candidate dashboards.
DATASOURCE_PLACEHOLDERS = {"DS_MIMIR": "Mimir", "DS_LOKI": "Loki"}
UNRESOLVED_MACRO = re.compile(r"\$__[_a-zA-Z0-9]+")
UNRESOLVED_VARIABLE = re.compile(r"\$(?:__[_a-zA-Z0-9]+|[_a-zA-Z][_a-zA-Z0-9]*)")
TIME_RANGE = re.compile(r"^now(?:-(?P<amount>[0-9]+)(?P<unit>[smhd]))?$")
BAD_BROWSER_TEXT = re.compile(
    r"(?:PromQL query has parsing errors|not a valid duration|data source error|"
    r"failed to load resource.*(?:/api/ds/query|api/ds/query))",
    re.IGNORECASE,
)


class VerificationError(RuntimeError):
    """A dashboard verification gate failed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dashboard", type=Path, help="candidate dashboard JSON")
    source.add_argument("--live-uid", help="UID of an already deployed dashboard")
    parser.add_argument("--grafana-url", default=DEFAULT_GRAFANA_URL)
    parser.add_argument("--secret", default=DEFAULT_SECRET, help="namespace/name of the Grafana admin Secret")
    parser.add_argument("--from", dest="from_range", default="now-1h")
    parser.add_argument("--to", dest="to_range", default="now")
    parser.add_argument("--screenshot-dir", type=Path, required=True)
    parser.add_argument("--browser-wait-seconds", type=float, default=10.0)
    parser.add_argument(
        "--allow-empty-panel",
        action="append",
        default=[],
        help="panel ID or exact title allowed to have no samples; explain this in the handoff",
    )
    parser.add_argument(
        "--allow-all-zero-panel",
        action="append",
        default=[],
        help="panel ID or exact title allowed to be entirely zero; explain this in the handoff",
    )
    return parser.parse_args()


def read_secret(secret_ref: str) -> tuple[str, str]:
    try:
        namespace, name = secret_ref.split("/", 1)
    except ValueError as exc:
        raise VerificationError("--secret must be namespace/name") from exc
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", name, "-o", "json"], check=True, capture_output=True, text=True
    )
    data = json.loads(result.stdout).get("data", {})
    try:
        user = base64.b64decode(data["admin-user"]).decode()
        password = base64.b64decode(data["admin-password"]).decode()
    except (KeyError, ValueError) as exc:
        raise VerificationError(f"Secret {secret_ref} lacks admin-user/admin-password") from exc
    return user, password


class GrafanaApi:
    def __init__(self, base_url: str, user: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=45.0, follow_redirects=True)
        response = self.client.post("/login", json={"user": user, "password": password})
        if response.status_code >= 400:
            raise VerificationError(f"Grafana login failed: HTTP {response.status_code}")

    def json_request(self, method: str, path: str, payload: Any | None = None) -> Any:
        response = self.client.request(method, path, json=payload)
        try:
            body = response.json()
        except ValueError:
            body = response.text[:500]
        if response.status_code >= 400:
            raise VerificationError(f"Grafana {method} {path} failed: HTTP {response.status_code}: {body}")
        return body

    def close(self) -> None:
        self.client.close()


def parse_time(value: str) -> int:
    match = TIME_RANGE.fullmatch(value)
    if not match:
        raise VerificationError(f"unsupported time expression {value!r}; use now or now-Ns/Nm/Nh/Nd")
    if match.group("amount") is None:
        return int(time.time() * 1000)
    seconds = int(match.group("amount")) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group("unit")]
    return int(time.time() * 1000) - seconds * 1000


def panels_in(value: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def visit(panel: dict[str, Any]) -> None:
        result.append(panel)
        for child in panel.get("panels", []):
            if isinstance(child, dict):
                visit(child)

    for panel in value.get("panels", []):
        if isinstance(panel, dict):
            visit(panel)
    return result


def panel_name(panel: dict[str, Any]) -> str:
    return f"{panel.get('id', '?')}:{panel.get('title', '<untitled>')}"


def matches_panel(panel: dict[str, Any], allow_list: list[str]) -> bool:
    return str(panel.get("id")) in allow_list or panel.get("title") in allow_list


def dashboard_variables(dashboard: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for variable in dashboard.get("templating", {}).get("list", []):
        if not isinstance(variable, dict) or not variable.get("name"):
            continue
        current = variable.get("current", {}).get("value")
        all_value = variable.get("allValue") or ".*"
        if isinstance(current, list):
            if "$__all" in current or "All" in current:
                values[variable["name"]] = all_value
            else:
                values[variable["name"]] = "|".join(str(item) for item in current)
        elif current in (None, "$__all", "All"):
            values[variable["name"]] = all_value
        else:
            values[variable["name"]] = str(current)
    return values


def expand_expression(expression: str, variables: dict[str, str]) -> str:
    if UNRESOLVED_MACRO.search(expression):
        raise VerificationError(f"unresolved Grafana macro in expression: {expression}")
    expanded = expression
    for name, value in sorted(variables.items(), key=lambda item: len(item[0]), reverse=True):
        expanded = expanded.replace("${" + name + "}", value)
        expanded = re.sub(r"\$" + re.escape(name) + r"(?![A-Za-z0-9_])", value, expanded)
    if UNRESOLVED_VARIABLE.search(expanded):
        raise VerificationError(f"unresolved Grafana variable/macro after expansion: {expanded}")
    return expanded


def datasource_map(api: GrafanaApi) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    entries = api.json_request("GET", "/api/datasources")
    by_name = {item["name"]: item for item in entries if item.get("name") and item.get("uid")}
    by_uid = {item["uid"]: item for item in entries if item.get("uid")}
    for name in DATASOURCE_PLACEHOLDERS.values():
        if name not in by_name:
            raise VerificationError(f"live Grafana datasource {name!r} was not found")
    return by_name, by_uid


def resolve_dashboard_datasources(value: Any, by_name: dict[str, dict[str, Any]], key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            item_key: resolve_dashboard_datasources(item_value, by_name, item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [resolve_dashboard_datasources(item, by_name, key) for item in value]
    if not isinstance(value, str):
        return value
    match = re.fullmatch(r"\$\{(DS_[A-Z0-9_]+)\}", value)
    if not match:
        return value
    placeholder = match.group(1)
    try:
        datasource = by_name[DATASOURCE_PLACEHOLDERS[placeholder]]
    except KeyError as exc:
        raise VerificationError(f"cannot resolve datasource placeholder {value}") from exc
    if key == "datasource":
        return {"type": datasource["type"], "uid": datasource["uid"]}
    return datasource["uid"]


def dashboard_from_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot load dashboard JSON {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("panels"), list):
        raise VerificationError(f"{path} is not a Grafana dashboard JSON document")
    return value


def dashboard_from_live(api: GrafanaApi, uid: str) -> dict[str, Any]:
    body = api.json_request("GET", f"/api/dashboards/uid/{uid}")
    dashboard = body.get("dashboard")
    if not isinstance(dashboard, dict):
        raise VerificationError(f"Grafana returned no dashboard for UID {uid}")
    return dashboard


def datasource_for(
    panel: dict[str, Any], target: dict[str, Any], by_name: dict[str, dict[str, Any]], by_uid: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    raw = target.get("datasource", panel.get("datasource"))
    if isinstance(raw, dict):
        uid = raw.get("uid")
        if uid in by_uid:
            return by_uid[uid]
    if isinstance(raw, str):
        placeholder = re.fullmatch(r"\$\{(DS_[A-Z0-9_]+)\}", raw)
        if placeholder:
            return by_name[DATASOURCE_PLACEHOLDERS[placeholder.group(1)]]
        if raw in by_uid:
            return by_uid[raw]
    raise VerificationError(f"{panel_name(panel)} target {target.get('refId', '?')} has no live datasource")


def has_frame_data(result: dict[str, Any]) -> tuple[bool, bool]:
    has_data = False
    numeric_values: list[float] = []
    for frame in result.get("frames", []):
        data = frame.get("data", {})
        values = data.get("values", [])
        fields = frame.get("schema", {}).get("fields", [])
        for index, column in enumerate(values):
            if not isinstance(column, list):
                continue
            field_name = str(fields[index].get("name", "")).lower() if index < len(fields) else ""
            for value in column:
                if value is None:
                    continue
                has_data = True
                if field_name in {"time", "timestamp", "ts"}:
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric_values.append(float(value))
                elif isinstance(value, str):
                    with contextlib.suppress(ValueError):
                        numeric_values.append(float(value))
    return has_data, bool(numeric_values) and all(value == 0 for value in numeric_values)


def query_panels(
    api: GrafanaApi,
    dashboard: dict[str, Any],
    by_name: dict[str, dict[str, Any]],
    by_uid: dict[str, dict[str, Any]],
    from_ms: int,
    to_ms: int,
    allow_empty: list[str],
    allow_all_zero: list[str],
) -> list[dict[str, Any]]:
    variables = dashboard_variables(dashboard)
    report: list[dict[str, Any]] = []
    for panel in panels_in(dashboard):
        if panel.get("type") == "text":
            continue
        targets = panel.get("targets", [])
        if not targets:
            report.append({"panel": panel_name(panel), "status": "skipped", "reason": "no targets"})
            continue
        for target in targets:
            expression = target.get("expr")
            if not isinstance(expression, str):
                raise VerificationError(f"{panel_name(panel)} target {target.get('refId', '?')} has no expr")
            expanded = expand_expression(expression, variables)
            datasource = datasource_for(panel, target, by_name, by_uid)
            query_target = copy.deepcopy(target)
            query_target["expr"] = expanded
            query_target["datasource"] = {"type": datasource["type"], "uid": datasource["uid"]}
            query_target.setdefault("refId", "A")
            query_target.setdefault("intervalMs", 15000)
            query_target.setdefault("maxDataPoints", 1000)
            body = {"queries": [query_target], "from": str(from_ms), "to": str(to_ms)}
            response = api.client.post("/api/ds/query", json=body)
            try:
                response_body = response.json()
            except ValueError:
                response_body = {"message": response.text[:500]}
            if response.status_code >= 400:
                raise VerificationError(
                    f"{panel_name(panel)} target {target.get('refId', '?')} returned "
                    f"HTTP {response.status_code}: {response_body}"
                )
            result = response_body.get("results", {}).get(query_target["refId"], {})
            if result.get("error"):
                raise VerificationError(f"{panel_name(panel)} target error: {result['error']}")
            has_data, all_zero = has_frame_data(result)
            report_item = {
                "panel": panel_name(panel),
                "refId": query_target["refId"],
                "datasource": datasource["name"],
                "frames": len(result.get("frames", [])),
                "has_data": has_data,
                "all_zero": all_zero,
                "status": "ok",
            }
            report.append(report_item)
            if not has_data and not matches_panel(panel, allow_empty):
                raise VerificationError(f"{panel_name(panel)} returned no frame values")
            if all_zero and not matches_panel(panel, allow_all_zero):
                raise VerificationError(f"{panel_name(panel)} returned only zero numeric values")
    return report


def dashboard_url(base_url: str, uid: str, dashboard: dict[str, Any], from_range: str, to_range: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", dashboard.get("title", "dashboard").lower()).strip("-") or "dashboard"
    variables = []
    for variable in dashboard.get("templating", {}).get("list", []):
        name = variable.get("name")
        if not name:
            continue
        current = variable.get("current", {}).get("value")
        if current in (None, "$__all") or (isinstance(current, list) and "$__all" in current):
            value = "All" if variable.get("includeAll") else str(current or "")
        elif isinstance(current, list):
            value = ",".join(str(item) for item in current)
        else:
            value = str(current)
        if value:
            variables.append((f"var-{name}", value))
    query = [("orgId", "1"), ("from", from_range), ("to", to_range), *variables]
    query_string = urlencode(query)
    return f"{base_url.rstrip('/')}/d/{uid}/{slug}?{query_string}"


def browser_verify(
    url: str, dashboard: dict[str, Any], screenshot_dir: Path, wait_seconds: float, user: str, password: str
) -> dict[str, Any]:
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    chrome = (
        str(Path(chromium_root) / "chrome-linux" / "headless_shell")
        if chromium_root
        else os.environ.get("GRAFANA_CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
    )
    if not chrome:
        raise VerificationError(
            "no Chrome/Chromium executable found; Bazel should set CHROMIUM_HEADLESS_SHELL "
            "or set GRAFANA_CHROME_PATH for a direct invocation"
        )
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot = screenshot_dir / f"{dashboard.get('uid', 'dashboard')}.png"
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    bad_responses: list[str] = []
    bad_query_payloads: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chrome, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page: Page = browser.new_page(viewport={"width": 1920, "height": 1200})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: failed_requests.append(f"{request.method} {request.url}: {request.failure}"),
        )
        page.on(
            "response",
            lambda response: (
                bad_responses.append(f"HTTP {response.status} {response.url}")
                if "/api/ds/query" in response.url and response.status >= 400
                else None
            ),
        )

        def inspect_request(request: Any) -> None:
            if "/api/ds/query" not in request.url or not request.post_data:
                return
            try:
                payload = json.loads(request.post_data)
            except json.JSONDecodeError:
                return
            for query in payload.get("queries", []):
                expression = query.get("expr", "")
                if UNRESOLVED_VARIABLE.search(expression):
                    bad_query_payloads.append(expression)

        page.on("request", inspect_request)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        user_input = page.locator('input[name="user"], input[autocomplete="username"]').first
        if user_input.count():
            user_input.fill(user)
            page.locator('input[name="password"], input[type="password"]').first.fill(password)
            page.locator('button[type="submit"]').first.click()
            page.wait_for_timeout(1000)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if "/login" in page.url:
            raise VerificationError("browser could not authenticate to Grafana")
        page.wait_for_timeout(int(wait_seconds * 1000))
        body_text = page.locator("body").inner_text()
        missing_titles = [
            panel_name(panel)
            for panel in panels_in(dashboard)
            if panel.get("type") != "text" and not page.get_by_text(panel.get("title", ""), exact=True).count()
        ]
        visible_no_data = sum(
            1
            for index in range(page.get_by_text("No data", exact=True).count())
            if page.get_by_text("No data", exact=True).nth(index).is_visible()
        )
        page.screenshot(path=str(screenshot), full_page=True)
        browser.close()

    print(f"screenshot={screenshot}")
    bad_console = [message for message in console_errors + page_errors if BAD_BROWSER_TEXT.search(message)]
    browser_errors: list[str] = []
    if bad_query_payloads:
        browser_errors.append(f"browser sent unresolved query expressions: {bad_query_payloads}")
    if failed_requests:
        browser_errors.append(f"browser request failures: {failed_requests}")
    if bad_responses:
        browser_errors.append(f"browser datasource responses failed: {bad_responses}")
    if bad_console:
        browser_errors.append(f"browser console/page errors: {bad_console}")
    if visible_no_data:
        browser_errors.append(f"browser visibly rendered {visible_no_data} 'No data' panel(s)")
    if missing_titles:
        browser_errors.append(f"dashboard panel titles were not rendered: {missing_titles}")
    if browser_errors:
        raise VerificationError("browser verification failures:\n- " + "\n- ".join(browser_errors))
    return {
        "screenshot": str(screenshot),
        "body_text_bytes": len(body_text.encode()),
        "console_errors": console_errors,
        "failed_requests": failed_requests,
    }


@contextlib.contextmanager
def temporary_dashboard(api: GrafanaApi, dashboard: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    temporary = copy.deepcopy(dashboard)
    uid = "verify-grafana-" + uuid.uuid4().hex[:12]
    temporary["id"] = None
    temporary["uid"] = uid
    temporary["title"] = "[verification] " + temporary.get("title", uid)
    temporary["version"] = 0
    response = api.json_request(
        "POST",
        "/api/dashboards/db",
        {"dashboard": temporary, "folderId": 0, "overwrite": False, "message": "temporary Grafana verification"},
    )
    try:
        yield uid, response
    finally:
        delete_response = api.client.delete(f"/api/dashboards/uid/{uid}")
        if delete_response.status_code not in (200, 404):
            raise VerificationError(f"failed to delete temporary dashboard {uid}: HTTP {delete_response.status_code}")


def main() -> int:
    args = parse_args()
    user, password = read_secret(args.secret)
    api = GrafanaApi(args.grafana_url, user, password)
    try:
        by_name, by_uid = datasource_map(api)
        if args.dashboard:
            dashboard = resolve_dashboard_datasources(dashboard_from_file(args.dashboard), by_name)
            dashboard_context: contextlib.AbstractContextManager[tuple[str | None, dict[str, Any] | None]] = (
                temporary_dashboard(api, dashboard)
            )
        else:
            dashboard_context = contextlib.nullcontext((None, None))
        with dashboard_context as temporary:
            if args.dashboard:
                temporary_uid, create_response = temporary
                assert temporary_uid is not None
                assert create_response is not None
                dashboard = dashboard_from_live(api, temporary_uid)
                print(f"temporary_dashboard={temporary_uid} url={create_response.get('url', '')}")
            else:
                dashboard = dashboard_from_live(api, args.live_uid)
            from_ms = parse_time(args.from_range)
            to_ms = parse_time(args.to_range)
            if from_ms >= to_ms:
                raise VerificationError("--from must be earlier than --to")
            query_report = query_panels(
                api, dashboard, by_name, by_uid, from_ms, to_ms, args.allow_empty_panel, args.allow_all_zero_panel
            )
            url = dashboard_url(args.grafana_url, dashboard["uid"], dashboard, args.from_range, args.to_range)
            browser_report = browser_verify(
                url, dashboard, args.screenshot_dir, args.browser_wait_seconds, user, password
            )
            print(json.dumps({"queries": query_report, "browser": browser_report}, indent=2, sort_keys=True))
            print("VERIFIED: datasource queries and authenticated browser rendering passed")
            return 0
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
