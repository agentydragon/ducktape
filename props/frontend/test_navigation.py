"""Exercise hash routing through the production app with a synthetic HTTP origin."""

from urllib.parse import quote, unquote, urlsplit

import pytest
import pytest_bazel
from playwright.async_api import Route, expect

from testing.frontend_visual.capture import VisualHarness

pytest_plugins = ("testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_app_hash_navigation(visual: VisualHarness) -> None:
    slug = "example/2026-01-01"
    file_path = "src/auth/login file.py"
    snapshot = {
        "slug": slug,
        "split": "valid",
        "created_at": "2026-01-01T00:00:00Z",
        "true_positives": [
            {
                "tp_id": "example-issue",
                "rationale": "Example issue",
                "occurrences": [
                    {"occurrence_id": "example-occurrence", "locations": [{"file": file_path, "start_line": 1}]}
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
        f"/api/gt/snapshots/{slug}/clusters": {"clusters": []},
        "/api/stats/occurrences": {"occurrences": []},
    }
    file_requests: list[str] = []

    async def respond(route: Route) -> None:
        path = urlsplit(route.request.url).path
        if "/files/" in path:
            file_requests.append(path)
            requested_file = unquote(path.split("/files/", maxsplit=1)[1])
            content = "# Routing example" if requested_file == "README.md" else "# Encoded routing example"
            await route.fulfill(json={"path": requested_file, "content": content, "line_count": 1})
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
        await view.page.get_by_role("tab", name="True Positives (1)").click()
        occurrence_link = view.page.get_by_role("link", name="example-issue/example-occurrence")
        await expect(occurrence_link).to_have_attribute(
            "href", f"#/snapshots/{slug}/example-issue/example-occurrence?file={quote(file_path, safe='')}"
        )
        await occurrence_link.click()
        await expect(view.page.get_by_text("# Encoded routing example", exact=True)).to_be_visible()
        assert file_requests[-1].endswith("/files/src/auth/login%20file.py")
        await view.page.go_back()
        await expect(view.page.get_by_text("# Routing example", exact=True)).to_be_visible()
        await view.capture("SnapshotHashQuery", target=view.page.locator(".mantine-AppShell-root"))
        await view.page.go_back()
        await view.page.go_back()
        await expect(view.page.get_by_role("cell", name=slug, exact=True)).to_be_visible()
        await view.page.go_forward()
        await expect(view.page.get_by_role("heading", name=slug, exact=True)).to_be_visible()


if __name__ == "__main__":
    pytest_bazel.main()
