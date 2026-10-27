"""Exercise hash routing through the production app with a synthetic HTTP origin."""

from urllib.parse import urlsplit

import pytest
import pytest_bazel
from playwright.async_api import Route, expect

from util.testing.visual_capture import VisualHarness

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_app_hash_navigation(visual: VisualHarness, capture_name: str) -> None:
    slug = "example/2026-01-01"
    snapshot = {
        "slug": slug,
        "split": "valid",
        "created_at": "2026-01-01T00:00:00Z",
        "true_positives": [
            {
                "tp_id": "example-issue",
                "rationale": "Example issue",
                "occurrences": [
                    {"occurrence_id": "example-occurrence", "locations": [{"file": "OTHER.md", "start_line": 1}]}
                ],
            }
        ],
        "false_positives": [],
    }
    responses = {
        "/auth/me": {"email": "viewer@example.test"},
        "/api/gt/snapshots": {"snapshots": [{**snapshot, "tp_count": 1, "fp_count": 0}]},
        f"/api/gt/snapshots/{slug}": snapshot,
        f"/api/gt/snapshots/{slug}/tree": {
            "tree": [{"path": "README.md", "name": "README.md", "is_dir": False, "tp_count": 0, "fp_count": 0}]
        },
        f"/api/gt/snapshots/{slug}/files/README.md": {
            "path": "README.md",
            "content": "# Routing example",
            "line_count": 1,
        },
        f"/api/gt/snapshots/{slug}/clusters": {"clusters": []},
        "/api/stats/occurrences": {"occurrences": []},
    }

    async def respond(route: Route) -> None:
        await route.fulfill(json=responses[urlsplit(route.request.url).path])

    async with visual.open(capture_name=capture_name) as view:
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
        await view.capture("SnapshotHashQuery", target=view.page.locator(".mantine-AppShell-root"))
        await view.page.go_back()
        await view.page.go_back()
        await expect(view.page.get_by_role("cell", name=slug, exact=True)).to_be_visible()
        await view.page.go_forward()
        await expect(view.page.get_by_role("heading", name=slug, exact=True)).to_be_visible()


if __name__ == "__main__":
    pytest_bazel.main()
