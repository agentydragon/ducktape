from __future__ import annotations

from collections.abc import Iterable
import re

from ember.evals.definitions import Scenario

CHECKLIST_PATTERN = re.compile(r"- \[[ xX]\]")


def verify_issue_comment(
    scenario: Scenario,
    *,
    issue: int = 1,
    required_keywords: Iterable[str] | None = None,
    require_checklist: bool = True,
    artifact: str | None = None,
):
    client = scenario.gitea()
    comments = client.issue_comments(issue)
    if not comments:
        scenario.fail(f"No comments found on issue #{issue}")

    expected = scenario.expected_gitea_author.lower()
    keywords = [kw.lower() for kw in (required_keywords or [])]

    matched_comment = None
    checklist_items = 0
    for comment in reversed(comments):
        author = comment.user.handle.lower()
        if author != expected:
            continue
        body_lower = comment.body.lower()
        if keywords and not all(keyword in body_lower for keyword in keywords):
            continue
        checklist_items = len(CHECKLIST_PATTERN.findall(comment.body))
        if require_checklist and checklist_items == 0:
            scenario.fail("Comment missing checklist items")
        matched_comment = comment
        break

    if matched_comment is None:
        scenario.fail(f"No matching comment from {scenario.expected_gitea_author}")

    if artifact:
        scenario.write_json_artifact(artifact, {"comments": [c.model_dump() for c in comments]})

    return scenario.ok(
        description="Verified Gitea issue comment",
        issue=issue,
        comment_id=matched_comment.id,
        matched_keywords=list(keywords),
        checklist_items=checklist_items,
    )


def verify_branch_file(scenario: Scenario, *, branch_template: str, file: str, contains: str, repo: str | None = None):
    branch_name = scenario.format(branch_template)
    client = scenario.gitea(repo)
    branch = client.branch_info(branch_name)
    content = client.file_contents(file, branch.sha)
    if contains not in content:
        scenario.fail(f"{file} on branch {branch_name} missing required content")
    return scenario.ok(description="Verified Gitea branch file", branch=branch_name, commit_sha=branch.sha, file=file)
