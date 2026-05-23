"""Public Augur browser smoke test against the Bazel-runnable server."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from base64 import urlsafe_b64decode
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ("util.playwright",)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright, Request


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    executable = str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None
    browser = playwright_sync.chromium.launch(
        headless=True,
        executable_path=executable,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        yield page
    finally:
        try:
            page.close()
        finally:
            context.close()


@pytest.fixture
def augur_server(tmp_path: Path) -> Iterator[str]:
    out = undeclared_outputs_dir()
    server_log = (out / "augur-server.log").open("w")
    port = pick_free_port("127.0.0.1")
    server = subprocess.Popen(
        [
            str(get_required_path("_main/augur/dev")),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--config",
            str(get_required_path("_main/augur/api/testdata/config.yaml")),
        ],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "MPLCONFIGDIR": str(tmp_path / "matplotlib"),
            "PYTHONUNBUFFERED": "1",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        stdout=server_log,
        stderr=server_log,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"Augur server exited early with code {server.returncode}; see {server_log.name}")
            try:
                with urllib.request.urlopen(f"{origin}/healthz", timeout=1) as response:
                    if response.status == 200 and response.read().decode() == "ok\n":
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            raise RuntimeError(f"Augur server did not start within 30s; see {server_log.name}")
        yield origin
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        server_log.close()


def _get_json(origin: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{origin}{path}", timeout=10) as response:
        assert response.status == 200
        assert "application/json" in response.headers["content-type"]
        return cast(dict[str, Any], json.loads(response.read().decode()))


def _post_json(origin: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{origin}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        assert "application/json" in response.headers["content-type"]
        return cast(dict[str, Any], json.loads(response.read().decode()))


def _assert_missing(origin: str, path: str, method: str = "GET") -> None:
    request = urllib.request.Request(f"{origin}{path}", data=b"{}" if method == "POST" else None, method=method)
    if method == "POST":
        request.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as error_info:
        urllib.request.urlopen(request, timeout=10)
    assert error_info.value.code == 404
    assert "application/json" in error_info.value.headers["content-type"]


def _decode_url_state(page: Page) -> dict[str, Any] | None:
    state = page.evaluate("() => new URL(window.location.href).searchParams.get('state')")
    if not state:
        return None
    padded_state = state + "=" * (-len(state) % 4)
    payload = json.loads(urlsafe_b64decode(padded_state.encode()).decode())
    return cast(dict[str, Any], payload)


def _wait_for_url_state(page: Page, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = _decode_url_state(page)
        if state is not None and predicate(state):
            return state
        time.sleep(0.1)
    raise AssertionError(
        f"timed out waiting for scenario URL state; saw:\n{json.dumps(_decode_url_state(page), indent=2)}"
    )


def _assert_context_panel_boundary(page: Page, selector: str) -> None:
    panel = page.locator(selector)
    panel.wait_for(state="visible", timeout=30_000)
    assert panel.count() == 1
    assert page.locator(f"{selector}[data-result-panel-kind]").count() == 0
    assert page.locator(f"{selector} [data-result-panel-kind]").count() == 0
    assert page.locator(f"[data-result-panel-kind] {selector}").count() == 0


def _assert_property_location_context_boundary(page: Page) -> None:
    _assert_context_panel_boundary(page, "[data-scenario-context-panel='property-location']")


def _assert_scenario_contract_context_boundary(page: Page) -> None:
    _assert_context_panel_boundary(page, "[data-scenario-context-panel='scenario-contract']")


def _assert_financing_tax_context_boundary(page: Page) -> None:
    _assert_context_panel_boundary(page, "[data-scenario-context-panel='financing-tax']")


def _assert_sampling_metadata_context_boundary(page: Page) -> None:
    _assert_context_panel_boundary(page, "[data-run-context-panel='exogenous-metadata']")


def _wait_for_successful_run(page: Page, *, scenario_requests: list[dict[str, Any]]) -> None:
    try:
        page.get_by_text("Exogenous model metadata").wait_for(state="visible", timeout=30_000)
    except Exception as error:
        run_error = page.get_by_text("Scenario-set run failed").first
        run_error_text = run_error.inner_text(timeout=1_000) if run_error.count() else "<none>"
        raise AssertionError(
            "Augur browser did not render a successful scenario-set run.\n"
            f"Run error: {run_error_text}\n"
            f"Body text: {page.evaluate('() => document.body.innerText')}\n"
            f"Scenario requests: {json.dumps(scenario_requests, indent=2)}"
        ) from error


def test_public_augur_shell_runs_against_fixture_config(page: Page, augur_server: str) -> None:
    _assert_missing(augur_server, "/api/cases")
    _assert_missing(augur_server, "/api/run", method="POST")
    _assert_missing(augur_server, "/api/projection/bootstrap")

    bootstrap = _get_json(augur_server, "/api/bootstrap")
    assert {property_["id"] for property_ in bootstrap["properties"]} == {"location_a_property", "location_b_property"}
    assert {location["id"] for location in bootstrap["locations"]} == {"location_a", "location_b"}
    bootstrap_json = json.dumps(bootstrap)
    assert "San Francisco" not in bootstrap_json
    assert "Vallejo" not in bootstrap_json
    assert "actor_policy_options" not in bootstrap
    assert "default_actor_policy" not in bootstrap
    assert "default_partner_monthly_payment_usd" not in bootstrap
    assert bootstrap["default_initial_checking_usd"] == 250000
    assert bootstrap["default_knobs"]["starting_portfolio_usd"] == 750000
    assert bootstrap["finance_snapshot"]["sp500_proxy_portfolio_usd"] == 750000
    assert bootstrap["finance_snapshot"]["concentrated_holdings"][0]["units"] == 1000

    scenario_run = _post_json(
        augur_server,
        "/api/scenario_sets/run",
        {
            "scenario_set_id": "public_browser_contract",
            "title": "Public browser contract",
            "sampling_request": {"rollout_count": 4, "horizon_months": 12, "seed": 11},
            "scenarios": [
                {
                    "scenario_id": "location_a",
                    "label": "Location A",
                    "actors": [{"actor_id": "agent_a", "label": "Agent A", "role": "primary_owner"}],
                    "property_selection": {"property_id": "location_a_property"},
                    "financing": {"financing_mode": "fixed_30", "down_payment_pct": 25, "mortgage_rate_pct": 6.5},
                    "initial_balance_sheet": {
                        "accounts": [
                            {
                                "account_id": "checking",
                                "account_type": "checking",
                                "owner_actor_id": "agent_a",
                                "balance_usd": 350_000,
                            }
                        ],
                        "assets": [],
                        "liabilities": [],
                    },
                    "policies": [],
                }
            ],
        },
    )
    result = scenario_run["scenario_results"][0]
    assert result["scenario_id"] == "location_a"
    assert "status" not in result
    assert result["metric_fan_columns"]["net_worth_usd"]["row_count"] == 13
    assert {"mortgage_payments", "property_purchases"} <= set(scenario_run["sampling_metadata"]["event_stream_ids"])

    page_errors: list[str] = []
    console_errors: list[str] = []
    bad_responses: list[str] = []
    request_urls: list[str] = []
    scenario_requests: list[dict[str, Any]] = []

    page.on("pageerror", lambda error: page_errors.append(error.message))
    page.on(
        "console",
        lambda message: (
            console_errors.append(message.text)
            if message.type == "error" and "Failed to load resource" not in message.text
            else None
        ),
    )
    page.on(
        "response",
        lambda response: (
            bad_responses.append(f"{response.status} {response.url}")
            if response.url.startswith(augur_server) and response.status >= 400
            else None
        ),
    )

    def record_request(request: Request) -> None:
        if not request.url.startswith(augur_server):
            return
        request_urls.append(request.url)
        if request.method == "POST" and request.url.endswith("/api/scenario_sets/run") and request.post_data:
            scenario_requests.append(json.loads(request.post_data))

    page.on("request", record_request)

    page.goto(augur_server, wait_until="domcontentloaded")
    page.get_by_role("heading", name="Augur", exact=True).wait_for(state="visible", timeout=15_000)
    page.get_by_text("Financial futures explorer").wait_for(state="visible", timeout=30_000)
    page.get_by_text("Distribution view").wait_for(state="visible", timeout=30_000)
    page.get_by_text("Terminal scenario comparison").wait_for(state="visible", timeout=30_000)
    assert page.evaluate("() => window.location.pathname") == "/distribution"
    assert page.get_by_text("Selected path monthly ledger").count() == 0
    assert page.locator("[data-result-panel-kind='distribution']").count() >= 2
    assert page.locator("[data-result-panel-kind='trajectory']").count() == 0
    assert page.locator("[data-result-panel-kind='accounting_detail']").count() == 0
    _wait_for_successful_run(page, scenario_requests=scenario_requests)
    _assert_sampling_metadata_context_boundary(page)
    _assert_scenario_contract_context_boundary(page)
    page.get_by_text("Event stream IDs").wait_for(state="hidden", timeout=30_000)
    page.get_by_text("Exogenous model metadata").click()
    page.get_by_text("Event stream IDs").wait_for(state="visible", timeout=30_000)
    page.get_by_text("Location A baseline").first.wait_for(state="visible", timeout=30_000)
    page.get_by_text("Location B baseline").first.wait_for(state="visible", timeout=30_000)
    page.get_by_text("Location A Property").first.wait_for(state="visible", timeout=30_000)
    _assert_property_location_context_boundary(page)
    _assert_financing_tax_context_boundary(page)
    page.get_by_text("No image").first.wait_for(state="visible", timeout=30_000)
    page.get_by_role("tab", name="Trajectory").click()
    page.get_by_text("Trajectory view").wait_for(state="visible", timeout=30_000)
    page.wait_for_function("() => window.location.pathname === '/trajectory'")
    page.get_by_text("Selected path monthly ledger").wait_for(state="visible", timeout=30_000)
    assert page.get_by_text("Terminal scenario comparison").count() == 0
    assert page.locator("[data-result-panel-kind='trajectory']").count() >= 3
    assert page.locator("[data-result-panel-kind='accounting_detail']").count() >= 1
    assert page.locator("[data-result-panel-kind='distribution']").count() == 0
    _assert_property_location_context_boundary(page)
    _assert_financing_tax_context_boundary(page)
    _assert_sampling_metadata_context_boundary(page)
    _assert_scenario_contract_context_boundary(page)
    assert page.evaluate("() => new URL(window.location.href).searchParams.get('rollout')") == "0"
    assert page.evaluate("() => new URL(window.location.href).searchParams.get('scenario')") == "scenario_1"

    page.get_by_label("Checking buffer").fill("100000")
    page.get_by_label("SP500-like portfolio").fill("200000")
    page.get_by_text("SP500 sales").first.wait_for(state="visible", timeout=30_000)

    page.get_by_label("Scenario property").select_option("location_b_property")
    page.get_by_role("heading", name="Location B Property").wait_for(state="visible", timeout=30_000)
    _assert_property_location_context_boundary(page)
    _assert_financing_tax_context_boundary(page)
    _assert_scenario_contract_context_boundary(page)
    page.get_by_label("Financing mode").select_option("custom")
    page.get_by_label("Down payment").fill("40")
    page.get_by_label("Custom mortgage rate").fill("7.35")
    page.get_by_label("Vacancy", exact=True).fill("9")
    page.get_by_label(re.compile("Private .* units")).fill("1000")

    rich_state = _wait_for_url_state(
        page,
        lambda state: bool(
            state.get("scenarios")
            and any(
                scenario["property_and_location"]["property_id"] == "location_b_property"
                and scenario["financing"]["financing_mode"] == "custom"
                and scenario["occupancy_and_rental"]["vacancy_pct"] == 9
                and scenario["initial_balance_sheet"]["private_equity_units"] == 1000
                and scenario["policies"]["private_equity_sale_policy"] == "none"
                for scenario in state["scenarios"]
            )
        ),
    )
    rich_scenario = next(
        scenario
        for scenario in rich_state["scenarios"]
        if scenario["property_and_location"]["property_id"] == "location_b_property"
        and scenario["financing"]["financing_mode"] == "custom"
    )
    assert rich_scenario["property_and_location"]["property_id"] == "location_b_property"
    assert rich_scenario["financing"]["financing_mode"] == "custom"
    assert rich_scenario["financing"]["down_payment_pct"] == 40
    assert rich_scenario["financing"]["custom_mortgage_rate"] == 7.35
    assert rich_scenario["occupancy_and_rental"]["vacancy_pct"] == 9
    assert rich_scenario["initial_balance_sheet"]["private_equity_units"] == 1000
    assert rich_scenario["policies"]["private_equity_sale_policy"] == "none"
    assert rich_scenario["policies"]["private_equity_liquid_net_worth_floor_usd"] == 0
    assert rich_scenario["policies"]["private_equity_tender_sale_amount_usd"] == 0
    assert "actors_and_ownership" not in rich_scenario

    assert page.get_by_text("Rai").count() == 0
    assert page.get_by_text("Auragon").count() == 0
    assert page.get_by_text("OpenAI").count() == 0
    assert page.get_by_text("San Francisco").count() == 0
    assert page.get_by_text("Vallejo").count() == 0
    assert page.get_by_text("Owner", exact=True).count() == 0
    assert page.get_by_text("Partner", exact=True).count() == 0
    assert any(url.endswith("/api/scenario_sets/run") for url in request_urls)
    assert not any("/api/run" in url or "/api/cases/" in url or "/api/projection/" in url for url in request_urls)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert bad_responses == []
    assert page_errors == []
    assert console_errors == []


def test_product_frontend_shell_can_jump_to_scenario_set_shell(page: Page, augur_server: str) -> None:
    page.goto(f"{augur_server}/product", wait_until="domcontentloaded")
    page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=15_000)
    page.get_by_text("Product projection").first.wait_for(state="visible", timeout=15_000)
    page.get_by_role("heading", name="Cash projection fan").first.wait_for(state="visible", timeout=15_000)
    page.get_by_label("Metric to plot").select_option("cash_usd")
    page.locator("[data-product-fan-chart='cashUsd']").wait_for(state="visible", timeout=30_000)
    page.get_by_label("Metric to plot").select_option("public_security_value_usd")
    page.locator("[data-product-fan-chart='publicSecurityValueUsd']").wait_for(state="visible", timeout=30_000)

    page.get_by_role("link", name="Scenario set").click()
    page.locator("[data-augur-surface='scenario-set']").wait_for(state="visible", timeout=15_000)
    page.wait_for_function("() => window.location.pathname === '/distribution'")
    page.get_by_text("Financial futures explorer").wait_for(state="visible", timeout=15_000)

    page.get_by_role("link", name="Product").click()
    page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=15_000)
    page.wait_for_function("() => window.location.pathname === '/product'")


if __name__ == "__main__":
    pytest_bazel.main()
