"""Exercise hash routing through the production app with a synthetic HTTP origin."""

from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
import pytest_bazel
from playwright.async_api import Route, expect

from testing.frontend_visual.capture import VisualHarness

pytest_plugins = ("testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_app_hash_navigation(visual: VisualHarness) -> None:
    slug = "example/2026-01-01"
    file_path = "src/auth/login #1?100%2F.py"
    snapshot = {
        "slug": slug,
        "split": "valid",
        "created_at": "2026-01-01T00:00:00Z",
        "true_positives": [
            {
                "tp_id": "example-issue",
                "rationale": "Example issue",
                "occurrences": [
                    {"occurrence_id": "example-occurrence", "locations": [{"file": file_path, "start_line": 95}]}
                ],
            },
            {
                "tp_id": "other-issue",
                "rationale": "Different issue with the same occurrence ID",
                "occurrences": [
                    {"occurrence_id": "example-occurrence", "locations": [{"file": file_path, "start_line": 2}]}
                ],
            },
        ],
        "false_positives": [],
    }
    responses = {
        "/auth/me": {"email": "viewer@example.test"},
        "/api/gt/snapshots": {"snapshots": [{**snapshot, "tp_count": 2, "fp_count": 0}]},
        f"/api/gt/snapshots/{slug}": snapshot,
        f"/api/gt/snapshots/{slug}/tree": {
            "tree": [{"path": "README.md", "name": "README.md", "is_dir": False, "tp_count": 0, "fp_count": 0}]
        },
        f"/api/gt/snapshots/{slug}/clusters": {"clusters": []},
        "/api/stats/occurrences": {"occurrences": []},
        "/api/stats/examples": {
            "snapshot_slug": slug,
            "example_kind": "file_set",
            "files_hash": "eefac986",
            "split": "valid",
            "recall_denominator": 1,
            "files": [file_path],
            "definitions": [],
            "credit_stats": None,
        },
    }
    file_requests: list[str] = []

    async def respond(route: Route) -> None:
        path = urlsplit(route.request.url).path
        if "/files/" in path:
            file_requests.append(path)
            requested_file = unquote(path.split("/files/", maxsplit=1)[1])
            lines = (
                ["# Routing example"]
                if requested_file == "README.md"
                else ["pass"] * 94 + ["# Encoded routing example"]
            )
            await route.fulfill(json={"path": requested_file, "content": "\n".join(lines), "line_count": len(lines)})
            return
        await route.fulfill(json=responses[path])

    async with visual.open() as view:
        await view.page.route("**/auth/me", respond)
        await view.page.route("**/api/**", respond)
        await view.page.evaluate("() => { window.location.hash = '/missing'; window.mountApp(); }")
        await expect(view.page.get_by_role("heading", name="Page Not Found")).to_be_visible()
        await view.page.get_by_role("link", name="Ground Truth", exact=True).click()
        await view.page.get_by_role("cell", name=slug, exact=True).click()
        await expect(view.page.get_by_role("heading", name=slug, exact=True)).to_be_visible()
        await view.page.evaluate(
            "slug => { window.location.hash = `/snapshots/${slug}/example-issue/example-occurrence?file=README.md`; }",
            slug,
        )
        await expect(view.page.get_by_text("# Routing example", exact=True)).to_be_visible()
        await view.page.get_by_role("tab", name="True Positives (2)").click()
        occurrence_link = view.page.get_by_role("link", name="example-issue/example-occurrence")
        await expect(occurrence_link).to_have_attribute(
            "href", f"#/snapshots/{slug}/example-issue/example-occurrence?file={quote(file_path, safe='')}"
        )
        await view.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        expected_url = await occurrence_link.evaluate("(link) => link.href")
        await view.page.get_by_role("button", name="Copy URL", exact=True).click()
        await expect(view.page.get_by_text("Copied!", exact=True)).to_be_visible()
        assert await view.page.evaluate("navigator.clipboard.readText()") == expected_url
        await occurrence_link.click()
        await expect(view.page.get_by_text("# Encoded routing example", exact=True)).to_be_visible()
        selected_marker = view.page.get_by_role("button", name="example-issue/example-occurrence TP")
        await expect(selected_marker).to_be_in_viewport()
        # Only the issue named in the link should carry the selection ring.
        highlighted = view.page.locator(".ring-2").get_by_role("button")
        await expect(highlighted).to_have_count(1)
        await expect(selected_marker.and_(highlighted)).to_be_visible()
        await view.page.evaluate('navigator.clipboard.writeText("")')
        await selected_marker.click()
        await view.page.get_by_role("button", name="Copy Link", exact=True).click()
        await expect(view.page.get_by_text("Copied!", exact=True)).to_be_visible()
        assert await view.page.evaluate("navigator.clipboard.readText()") == expected_url
        assert file_requests[-1] == f"/api/gt/snapshots/{slug}/files/{quote(file_path)}"
        await view.page.go_back()
        await expect(view.page.get_by_text("# Routing example", exact=True)).to_be_visible()
        await view.capture("SnapshotHashQuery", target=view.page.locator(".mantine-AppShell-root"))
        await view.page.go_back()
        await view.page.go_back()
        await expect(view.page.get_by_role("cell", name=slug, exact=True)).to_be_visible()
        await view.page.go_forward()
        await expect(view.page.get_by_role("heading", name=slug, exact=True)).to_be_visible()

        await view.page.evaluate(
            "hash => { window.location.hash = hash; }",
            f"/examples?snapshot_slug={slug}&example_kind=file_set&files_hash=eefac986",
        )
        await view.page.get_by_role("link", name=file_path, exact=True).click()
        await expect(view.page.get_by_text("# Encoded routing example", exact=True)).to_be_visible()


async def test_snapshot_rejected_token_returns_to_login(visual: VisualHarness) -> None:
    snapshot = "**/api/gt/snapshots/example/expired"
    async with visual.open() as view:
        await view.page.route(snapshot + "/tree", lambda route: route.fulfill(json={"tree": []}))
        await view.page.route(snapshot + "/clusters", lambda route: route.fulfill(json={"clusters": []}))
        await view.page.route(snapshot, lambda route: route.fulfill(status=401, json={"detail": "Expired token"}))
        async with view.page.expect_request(snapshot) as request:
            await view.page.evaluate(
                """() => {
                    history.replaceState(null, "", "/?token=expired-token#/snapshots/example/expired");
                    window.mountApp();
                }"""
            )
        assert await (await request.value).header_value("authorization") == "Bearer expired-token"
        await expect(view.page.get_by_role("link", name="Sign in with Authentik")).to_be_visible()


async def test_runs_browser_sends_selected_filters(visual: VisualHarness) -> None:
    async with visual.open() as view:
        await view.page.route("**/auth/me", lambda route: route.fulfill(json={"email": "viewer@example.test"}))
        await view.page.route("**/api/runs?*", lambda route: route.fulfill(json={"runs": [], "total_count": 0}))
        await view.page.evaluate("() => { window.location.hash = '/runs'; window.mountApp(); }")
        await expect(view.page.get_by_text("No runs found")).to_be_visible()
        await view.page.get_by_role("combobox", name="Status", exact=True).click()
        async with view.page.expect_request("**/api/runs?*"):
            await view.page.get_by_role("option", name="timed out", exact=True).click()

        await view.page.get_by_role("combobox", name="Agent Type", exact=True).click()
        async with view.page.expect_request("**/api/runs?*") as request:
            await view.page.get_by_role("option", name="grader", exact=True).click()
        params = parse_qs(urlsplit((await request.value).url).query)
        assert params["status"] == ["timed_out"]
        assert params["agent_type"] == ["grader"]


if __name__ == "__main__":
    pytest_bazel.main()
