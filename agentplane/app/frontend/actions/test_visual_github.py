"""github Action presentation examples."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.actions.testing.action_presentation_cases import ActionExample, ActionPresentationCases
from agentplane.app.frontend.testing.app import AgentplaneFixture
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE
from testing.frontend_visual.capture import VisualPage
from testing.frontend_visual.page_capture import wait_for_stable
from testing.frontend_visual.viewports import MOBILE, Viewport

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

GITHUB_REPO: dict[str, object] = {"owner": "example", "repo": "service"}

GITHUB_PULL = {**GITHUB_REPO, "pullNumber": 42}

GITHUB_ISSUE = {**GITHUB_REPO, "issue_number": 7}

GITHUB_PULL_ROW = {
    "number": 42,
    "title": "Document deployment checks",
    "state": "open",
    "draft": True,
    "merged": False,
    "html_url": "https://github.com/example/service/pull/42",
    "head": {"ref": "docs"},
    "base": {"ref": "devel"},
}

GITHUB_ISSUE_ROW = {
    "number": 7,
    "title": "Review staging readiness",
    "state": "OPEN",
    "html_url": "https://github.com/example/service/issues/7",
    "labels": ["deployments"],
    "assignees": ["test-reviewer"],
}

GITHUB_COMMIT = {
    "sha": "0123456789abcdef0123456789abcdef01234567",
    "html_url": "https://github.com/example/service/commit/0123456789abcdef0123456789abcdef01234567",
    "commit": {
        "message": "Document readiness checks",
        "author": {"name": "Test maintainer", "date": "2026-11-19T18:00:00Z"},
    },
}

GITHUB_RELEASE = {
    "id": 15,
    "tag_name": "v1.2.0",
    "name": "Rollout tools 1.2",
    "html_url": "https://github.com/example/service/releases/tag/v1.2.0",
    "published_at": "2026-11-19T18:00:00Z",
    "prerelease": False,
    "draft": False,
    "body": "Adds staging readiness and rollback checks.",
}

GITHUB_USER = {"login": "test-reviewer", "profile_url": "https://github.com/test-reviewer"}


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "github",
            "push_files",
            {
                "owner": "example",
                "repo": "service",
                "branch": "docs",
                "message": "docs: document deployment checks",
                "files": [
                    {
                        "path": "docs/deployment.md",
                        "content": "# Deployment checks\n\nWait for readiness before shifting traffic.\n",
                    },
                    {
                        "path": "deploy/checks.py",
                        "content": "def ready(status: str) -> bool:\n    return status == 'ok'\n",
                    },
                ],
            },
            {"ref": "refs/heads/docs", "object": {"sha": "0123456789abcdef0123456789abcdef01234567", "type": "commit"}},
            "Pushed to docs.",
        ),
        ActionExample(
            "github",
            "delete_file",
            {
                "owner": "example",
                "repo": "service",
                "path": "docs/old-guide.md",
                "message": "docs: remove old deployment guide",
                "branch": "docs",
            },
            {"commit": {"sha": "0123456789abcdef0123456789abcdef01234567"}, "content": None},
            "Deleted file.",
            compact_card_expands=False,
        ),
        ActionExample(
            "github",
            "update_pull_request",
            {
                "owner": "example",
                "repo": "service",
                "pullNumber": 42,
                "state": "closed",
                "body": "Closed in favor of a smaller change.\nThe readiness checks move to a follow-up.",
            },
            {"id": "4200", "url": "https://github.com/example/service/pull/42"},
            "Pull request updated.",
        ),
        ActionExample(
            "github",
            "update_pull_request_branch",
            {"owner": "example", "repo": "service", "pullNumber": 42, "expectedHeadSha": "0123456789abcdef"},
            "Pull request branch update is in progress",
            "Update queued",
        ),
        ActionExample(
            "github",
            "create_or_update_file",
            {
                "owner": "example",
                "repo": "service",
                "path": "docs/deployment.md",
                "content": "# Deployment checks\n\nWait for readiness before shifting traffic.\n",
                "message": "docs: document deployment checks",
                "branch": "docs",
                "allow_symlink_write": False,
            },
            {
                "commit": {
                    "sha": "0123456789abcdef0123456789abcdef01234567",
                    "html_url": "https://github.com/example/service/commit/0123456789abcdef0123456789abcdef01234567",
                },
                "content": {
                    "name": "deployment.md",
                    "path": "docs/deployment.md",
                    "sha": "abcdef0123456789abcdef0123456789abcdef01",
                    "html_url": "https://github.com/example/service/blob/docs/docs/deployment.md",
                },
            },
            "Committed file changes.",
        ),
        ActionExample(
            "github",
            "create_branch",
            {"owner": "example", "repo": "service", "branch": "docs", "from_branch": "devel"},
            {"ref": "refs/heads/docs", "object": {"sha": "0123456789abcdef0123456789abcdef01234567", "type": "commit"}},
            "Created branch docs.",
            compact_card_expands=False,
        ),
        ActionExample(
            "github",
            "create_pull_request",
            {
                "owner": "example",
                "repo": "service",
                "title": "Document deployment checks",
                "head": "docs",
                "base": "devel",
                "body": "Explain readiness checks.\nInclude rollback instructions.",
                "draft": True,
                "maintainer_can_modify": False,
                "reviewers": ["reviewer"],
            },
            {"id": "4200", "html_url": "https://github.com/example/service/pull/42"},
            "Pull request created.",
        ),
        ActionExample(
            "github",
            "search_pull_requests",
            {
                "owner": "example",
                "repo": "service",
                "query": "is:open label:bug",
                "page": 2,
                "perPage": 10,
                "sort": "updated",
                "order": "desc",
                "fields": ["number", "title", "state", "html_url"],
            },
            {
                "total_count": 11,
                "incomplete_results": True,
                "items": [
                    {
                        "number": 42,
                        "title": "Handle deployment checks",
                        "state": "open",
                        "html_url": "https://github.com/example/service/pull/42",
                    }
                ],
            },
            "Handle deployment checks",
        ),
        ActionExample(
            "github",
            "list_pull_requests",
            {**GITHUB_REPO, "state": "open", "base": "devel"},
            [GITHUB_PULL_ROW],
            "Document deployment checks",
        ),
        ActionExample(
            "github",
            "pull_request_read",
            {**GITHUB_PULL, "method": "get_review_comments", "perPage": 10},
            {
                "review_threads": [
                    {
                        "id": "PRRT_test",
                        "is_resolved": False,
                        "is_outdated": False,
                        "total_count": 1,
                        "comments": [
                            {
                                "path": "docs/deploy.md",
                                "line": 12,
                                "author": "test-reviewer",
                                "body": "Please document the rollback.",
                                "html_url": "https://github.com/example/service/pull/42#discussion_r81",
                            }
                        ],
                    }
                ],
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False, "hasPreviousPage": False},
            },
            "Please document the rollback.",
        ),
        ActionExample(
            "github",
            "merge_pull_request",
            {
                **GITHUB_PULL,
                "merge_method": "squash",
                "expectedHeadSha": "0123456789abcdef",
                "commit_title": "Document deployment checks",
            },
            {
                "sha": "0123456789abcdef0123456789abcdef01234567",
                "merged": True,
                "message": "Pull Request successfully merged",
            },
            "Pull request merged.",
        ),
        ActionExample(
            "github", "request_copilot_review", GITHUB_PULL, {"status": "requested"}, "Copilot review requested."
        ),
        ActionExample(
            "github",
            "pull_request_review_write",
            {**GITHUB_PULL, "method": "submit_pending", "event": "REQUEST_CHANGES", "body": "Add a rollback section."},
            "pending pull request review successfully submitted",
            "pending pull request review successfully submitted",
        ),
        ActionExample(
            "github",
            "add_comment_to_pending_review",
            {
                **GITHUB_PULL,
                "path": "docs/deploy.md",
                "subjectType": "LINE",
                "line": 12,
                "side": "RIGHT",
                "body": "Please indicate the previous revision.",
            },
            "pull request review comment successfully added to pending review",
            "pull request review comment successfully added to pending review",
        ),
        ActionExample(
            "github",
            "add_reply_to_pull_request_comment",
            {**GITHUB_PULL, "commentId": 81, "body": "Added the rollback instructions.", "reaction": "+1"},
            {
                "comment": {"id": "82", "url": "https://github.com/example/service/pull/42#discussion_r82"},
                "reaction": {"id": "91"},
            },
            "Reply and reaction posted",
        ),
        ActionExample(
            "github",
            "issue_read",
            {**GITHUB_ISSUE, "method": "get"},
            {
                "method": "get",
                "issue": {**GITHUB_ISSUE_ROW, "state": "open", "body": "Confirm readiness on the staging environment."},
            },
            "Confirm readiness on the staging environment.",
        ),
        ActionExample(
            "github",
            "list_issues",
            {
                **GITHUB_REPO,
                "state": "OPEN",
                "labels": ["deployments"],
                "field_filters": [{"field_name": "Priority", "value": "P1"}],
            },
            {
                "issues": [GITHUB_ISSUE_ROW],
                "totalCount": 4,
                "pageInfo": {"hasNextPage": True, "hasPreviousPage": False},
            },
            "More results available.",
        ),
        ActionExample(
            "github",
            "search_issues",
            {"query": "repo:example/service is:open label:deployments"},
            {"items": [GITHUB_ISSUE_ROW], "total_count": 1, "incomplete_results": False},
            "Review staging readiness",
        ),
        ActionExample(
            "github",
            "issue_write",
            {
                **GITHUB_ISSUE,
                "method": "update",
                "title": "Review staging readiness",
                "state": "closed",
                "state_reason": "completed",
                "labels": ["deployments"],
                "issue_fields": [{"field_name": "Priority", "field_option_name": "P1"}],
            },
            {"id": "7700", "url": "https://github.com/example/service/issues/7"},
            "Issue updated.",
        ),
        ActionExample(
            "github",
            "sub_issue_write",
            {**GITHUB_ISSUE, "method": "add", "sub_issue_id": 7800, "replace_parent": True},
            {
                "method": "add",
                "issue": {
                    "number": 8,
                    "title": "Document rollback checks",
                    "html_url": "https://github.com/example/service/issues/8",
                    "assignees": [],
                },
            },
            "Sub-issue linked.",
        ),
        ActionExample(
            "github",
            "add_issue_comment",
            {**GITHUB_ISSUE, "body": "Staging is ready for review.", "reaction": "rocket"},
            {
                "comment": {"id": "70", "url": "https://github.com/example/service/issues/7#issuecomment-70"},
                "reaction": {"id": "71"},
            },
            "Comment and reaction posted",
        ),
        ActionExample(
            "github",
            "update_issue_comment",
            {**GITHUB_REPO, "comment_id": 70, "body": "Staging and rollback checks both passed."},
            {"id": "70", "html_url": "https://github.com/example/service/issues/7#issuecomment-70"},
            "Comment updated",
        ),
        ActionExample(
            "github",
            "list_issue_fields",
            GITHUB_REPO,
            [
                {
                    "id": "IF_priority",
                    "name": "Priority",
                    "data_type": "SINGLE_SELECT",
                    "visibility": "PRIVATE",
                    "options": [{"id": "IF_opt", "name": "P1", "color": "ORANGE"}],
                }
            ],
            "Options P1",
        ),
        ActionExample(
            "github",
            "list_issue_types",
            GITHUB_REPO,
            [{"id": 1, "name": "Bug", "description": "Unexpected application behavior", "is_enabled": True}],
            "Unexpected application behavior",
        ),
        ActionExample(
            "github",
            "get_label",
            {**GITHUB_REPO, "name": "deployments"},
            {"name": "deployments", "description": "Release and rollout work", "color": "0366d6"},
            "Release and rollout work",
        ),
        ActionExample(
            "github",
            "create_repository",
            {
                "name": "demo-rollout",
                "organization": "example",
                "private": True,
                "autoInit": True,
                "description": "Tools for staging rollout",
            },
            {"id": "13", "url": "https://github.com/example/demo-rollout"},
            "Repository created",
        ),
        ActionExample(
            "github",
            "fork_repository",
            {**GITHUB_REPO, "organization": "test-destination"},
            "Fork is in progress",
            "Fork accepted",
        ),
        ActionExample(
            "github",
            "get_file_contents",
            {**GITHUB_REPO, "path": "docs", "ref": "refs/heads/devel"},
            [
                {
                    "name": "readiness.md",
                    "path": "docs/readiness.md",
                    "type": "file",
                    "size": 120,
                    "html_url": "https://github.com/example/service/blob/devel/docs/readiness.md",
                }
            ],
            "docs/readiness.md",
        ),
        ActionExample(
            "github",
            "get_commit",
            {**GITHUB_REPO, "sha": GITHUB_COMMIT["sha"], "detail": "full_patch"},
            {
                **GITHUB_COMMIT,
                "stats": {"additions": 1, "deletions": 0},
                "files": [
                    {
                        "filename": "docs/readiness.md",
                        "status": "added",
                        "additions": 1,
                        "patch": "@@ -0,0 +1 @@\n+Check readiness.",
                    }
                ],
            },
            "Check readiness.",
        ),
        ActionExample(
            "github",
            "list_commits",
            {**GITHUB_REPO, "sha": "devel", "path": "docs", "perPage": 5},
            [GITHUB_COMMIT],
            "Document readiness checks",
        ),
        ActionExample(
            "github",
            "list_branches",
            {**GITHUB_REPO, "perPage": 5},
            [
                {"name": "devel", "sha": GITHUB_COMMIT["sha"], "protected": True},
                {"name": "docs", "sha": GITHUB_COMMIT["sha"], "protected": False},
            ],
            "Protected",
        ),
        ActionExample("github", "list_tags", GITHUB_REPO, [{"name": "v1.2.0", "sha": GITHUB_COMMIT["sha"]}], "v1.2.0"),
        ActionExample(
            "github",
            "get_tag",
            {**GITHUB_REPO, "tag": "v1.2.0"},
            {"ref": "refs/tags/v1.2.0", "object": {"type": "commit", "sha": GITHUB_COMMIT["sha"]}},
            "Lightweight tag",
        ),
        ActionExample("github", "list_releases", {**GITHUB_REPO, "perPage": 5}, [GITHUB_RELEASE], "Rollout tools 1.2"),
        ActionExample("github", "get_latest_release", GITHUB_REPO, GITHUB_RELEASE, "Adds staging readiness"),
        ActionExample(
            "github", "get_release_by_tag", {**GITHUB_REPO, "tag": "v1.2.0"}, GITHUB_RELEASE, "Rollout tools 1.2"
        ),
        ActionExample(
            "github",
            "search_commits",
            {"query": "repo:example/service readiness", "perPage": 5},
            {
                "total_count": 1,
                "incomplete_results": False,
                "items": [{**GITHUB_COMMIT, "repository": {"full_name": "example/service"}}],
            },
            "Document readiness checks",
        ),
        ActionExample(
            "github",
            "get_me",
            {},
            {**GITHUB_USER, "details": {"name": "Test reviewer", "company": "Example organization"}},
            "Test reviewer",
            compact_card_expands=False,
        ),
        ActionExample(
            "github",
            "get_team_members",
            {"org": "example", "team_slug": "release-reviewers"},
            ["test-reviewer"],
            "test-reviewer",
        ),
        ActionExample(
            "github",
            "get_teams",
            {"user": "test-reviewer"},
            [
                {
                    "org": "example",
                    "teams": [
                        {
                            "name": "Release reviewers",
                            "slug": "release-reviewers",
                            "description": "Review staging changes",
                        }
                    ],
                }
            ],
            "Release reviewers",
            compact_card_expands=False,
        ),
        ActionExample(
            "github",
            "list_repository_collaborators",
            {**GITHUB_REPO, "affiliation": "direct"},
            {
                "firstPage": 1,
                "lastPage": 3,
                "nextPage": 2,
                "prevPage": 0,
                "items": [{"login": "test-reviewer", "id": 8, "role_name": "triage"}],
            },
            "Triage",
        ),
        ActionExample(
            "github",
            "search_code",
            {"query": "repo:example/service readiness", "perPage": 5},
            {
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "name": "readiness.md",
                        "path": "docs/readiness.md",
                        "sha": GITHUB_COMMIT["sha"],
                        "repository": "example/service",
                        "text_matches": [{"fragment": "Wait for staging readiness."}],
                    }
                ],
            },
            "Wait for staging readiness.",
        ),
        ActionExample(
            "github",
            "search_repositories",
            {"query": "org:example rollout", "minimal_output": True},
            {
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "id": 10,
                        "name": "service",
                        "full_name": "example/service",
                        "html_url": "https://github.com/example/service",
                        "description": "Staging rollout tools",
                        "language": "Go",
                        "stargazers_count": 12,
                    }
                ],
            },
            "Staging rollout tools",
            compact_card_expands=False,
        ),
        ActionExample(
            "github",
            "search_users",
            {"query": "org:example reviewer"},
            {"total_count": 1, "incomplete_results": False, "items": [GITHUB_USER]},
            "test-reviewer",
            compact_card_expands=False,
        ),
        ActionExample(
            "github",
            "actions_get",
            {**GITHUB_REPO, "method": "get_workflow_job", "resource_id": "208"},
            {
                "method": "get_workflow_job",
                "workflow_job": {
                    "id": 208,
                    "run_id": 81,
                    "name": "Check staging readiness",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/example/service/actions/runs/81/job/208",
                    "steps": [
                        {"name": "Verify readiness", "number": 1, "status": "completed", "conclusion": "failure"}
                    ],
                },
            },
            "Verify readiness",
        ),
        ActionExample(
            "github",
            "actions_list",
            {
                **GITHUB_REPO,
                "method": "list_workflow_runs",
                "resource_id": "rollout.yml",
                "workflow_runs_filter": {"branch": "devel", "status": "completed"},
            },
            {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 81,
                        "name": "Rollout checks",
                        "display_title": "Check staging deployment",
                        "workflow_id": 4,
                        "run_number": 5,
                        "run_attempt": 1,
                        "status": "completed",
                        "conclusion": "failure",
                        "head_branch": "devel",
                        "html_url": "https://github.com/example/service/actions/runs/81",
                    }
                ],
            },
            "Check staging deployment",
        ),
        ActionExample(
            "github",
            "actions_run_trigger",
            {**GITHUB_REPO, "method": "rerun_failed_jobs", "run_id": 81},
            {
                "message": "Failed jobs have been queued for re-run",
                "run_id": 81,
                "status": "201 Created",
                "status_code": 201,
            },
            "Rerun queued",
        ),
        ActionExample(
            "github",
            "get_job_logs",
            {**GITHUB_REPO, "run_id": 81, "failed_only": True, "return_content": True, "tail_lines": 25},
            {
                "run_id": 81,
                "failed_jobs": 1,
                "total_jobs": 2,
                "message": "Retrieved logs for 1 failed jobs",
                "logs": [
                    {
                        "job_id": 208,
                        "job_name": "Check staging readiness",
                        "logs_content": "The health endpoint returned 503.",
                        "message": "Log content",
                        "original_length": 100,
                    }
                ],
                "return_format": {"content": True, "urls": False},
            },
            "The health endpoint returned 503.",
        ),
        ActionExample(
            "github",
            "run_secret_scanning",
            {**GITHUB_REPO, "files": ["EXAMPLE_CONFIG=stub", "EXAMPLE_TOKEN=fixture-only"]},
            "Hosted scanner receipt fixture",
            "View submitted content and scan findings in Raw.",
        ),
        ActionExample(
            "github",
            "ui_get",
            {**GITHUB_REPO, "method": "reviewers"},
            {
                "method": "reviewers",
                "reviewers": {
                    "users": [{"login": "test-reviewer", "avatar_url": ""}],
                    "teams": [{"org": "example", "name": "Release reviewers", "slug": "release-reviewers"}],
                    "totalCount": 2,
                    "has_more": False,
                },
            },
            "Release reviewers",
        ),
    )


@DESKTOP_AND_MOBILE
async def test_github_push_compact_diff(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    args: dict[str, object] = {
        "owner": "example",
        "repo": "service",
        "branch": "docs",
        "message": "docs: update the deployment guide",
        "files": [
            {"path": "docs/deploy.md", "content": "# Deploy\n\nCheck readiness.\nKeep rollback ready.\n"},
            {"path": "docs/verify.md", "content": "# Verify\n\nCheck the logs.\n"},
        ],
    }
    await app.show_action_preview(
        "github",
        "push_files",
        args,
        {
            "github-push-files-preview": {
                "baseCommitSha": sha,
                "files": [
                    {"path": "docs/deploy.md", "content": "# Deploy\n\nWait 30 seconds.\nKeep rollback ready.\n"}
                ],
            }
        },
    )
    await app.show_action_receipt(
        "github",
        "push_files",
        args,
        {"content": [], "structuredContent": {"ref": "refs/heads/docs", "object": {"sha": sha, "type": "commit"}}},
    )
    sidebar_width = (viewport.width if viewport == MOBILE else 240) - 16
    detail_width = viewport.width - (0 if viewport == MOBILE else 240) - 32
    await app.mount_action_presentations(sidebar_width=sidebar_width, detail_width=detail_width, mcp=True)
    sidebar = view.page.locator("[data-action-visual=sidebar]")
    await sidebar.get_by_role("button", name="Expand ", exact=False).click()
    await expect(sidebar.get_by_role("link", name="0123456")).to_have_attribute(
        "href", f"https://github.com/example/service/commit/{sha}"
    )
    changed_file = sidebar.get_by_role("button", name="docs/deploy.md · +1 -1", exact=True)
    unverified_name = "docs/verify.md · 26 B"
    unverified_file = sidebar.get_by_role("button", name=unverified_name, exact=True)
    await expect(changed_file).to_have_attribute("aria-expanded", "false")
    await expect(unverified_file).to_have_attribute("aria-expanded", "false")
    unverified_frame = sidebar.locator("[data-layout=disclosure]").filter(
        has=view.page.get_by_role("button", name=unverified_name, exact=True)
    )
    await unverified_frame.scroll_into_view_if_needed()
    await expect(changed_file).to_be_in_viewport(ratio=0.95)
    await expect(unverified_file).to_be_in_viewport(ratio=0.95)
    await view.capture("files", target=sidebar)

    await changed_file.click()
    added = sidebar.get_by_text("+Check readiness.", exact=True)
    await added.scroll_into_view_if_needed()
    await expect(sidebar.get_by_text("-Wait 30 seconds.", exact=True)).to_be_in_viewport()
    await expect(added).to_be_in_viewport()
    await view.capture("diff", target=sidebar)

    approve = sidebar.get_by_role("button", name="Approve ", exact=False)
    await approve.scroll_into_view_if_needed()
    await expect(approve).to_be_in_viewport()
    await expect(sidebar.get_by_role("button", name="Deny ", exact=False)).to_be_in_viewport()
    await view.capture("decision", target=sidebar)

    await sidebar.get_by_role("button", name="Proposed contents", exact=True).click()
    await expect(sidebar.get_by_text("Check readiness.", exact=True)).to_be_visible()
    await unverified_file.click()
    unverified_body = sidebar.get_by_role("region", name=unverified_name, exact=True)
    await unverified_body.scroll_into_view_if_needed()
    await expect(unverified_body.get_by_text("Check the logs.", exact=True)).to_be_visible()


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("mixed", [False, True], ids=["labels-only", "mixed"])
async def test_issue_update_compact(view: VisualPage, app: AgentplaneFixture, viewport: Viewport, mixed: bool) -> None:
    args = {**GITHUB_ISSUE, "method": "update", "labels": ["bug", "upstream-waiting", "needs investigation", "triage"]}
    if mixed:
        args.update(
            title="Verify staging readiness", assignees=["alex", "taylor"], state="closed", state_reason="not_planned"
        )
    await app.show_action_preview(
        "github",
        "issue_write",
        args,
        {
            "github-label-summaries": {
                "labels": [
                    {"name": "bug", "color": "d73a4a"},
                    {"name": "upstream-waiting", "color": "0052cc"},
                    {"name": "needs investigation", "color": "fbca04"},
                ]
            }
        },
    )
    await app.show_action_receipt(
        "github", "issue_write", args, {"content": [], "structuredContent": {"id": "7700"}, "isError": False}
    )
    sidebar_width = (viewport.width if viewport == MOBILE else 240) - 16
    detail_width = viewport.width - (0 if viewport == MOBILE else 240) - 32
    await app.mount_action_presentations(sidebar_width=sidebar_width, detail_width=detail_width, mcp=True)
    sidebar = view.page.locator("[data-action-visual=sidebar]")
    issue = sidebar.get_by_role("link", name="example/service#7", exact=True)
    await expect(issue).to_have_attribute("href", "https://github.com/example/service/issues/7")
    if mixed:
        await expect(sidebar).to_contain_text("Close example/service#7")
        await expect(sidebar).to_contain_text("New title Verify staging readiness")
        await expect(sidebar).to_contain_text("Reason Not planned")
        await expect(sidebar).to_contain_text("Set assignees to alex, taylor")
        await expect(sidebar).to_contain_text("Set labels to")
    badges = sidebar.locator(".mantine-Badge-root")
    await expect(badges.filter(has_text="bug")).to_have_css("background-color", "rgb(215, 58, 74)")
    await expect(badges.filter(has_text="upstream-waiting")).to_have_css("color", "rgb(255, 255, 255)")
    await expect(badges.filter(has_text="needs investigation")).to_have_css("color", "rgb(0, 0, 0)")
    await expect(badges.filter(has_text="triage")).to_be_visible()
    await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_be_visible()
    await expect(sidebar.get_by_role("button", name="Deny ", exact=False)).to_be_visible()
    await expect(sidebar.get_by_role("button", name="Expand ", exact=False)).to_have_count(0)
    await wait_for_stable(view.page)
    await view.capture(target=sidebar)


if __name__ == "__main__":
    pytest_bazel.main()
