from __future__ import annotations

import re
from collections.abc import Iterable

from ember.evals.definitions import Scenario
from ember.integrations.gitea import GiteaClient, GiteaRepository

CHECKLIST_PATTERN = re.compile(r"- \[[ xX]\]")


def _expected_author(scenario: Scenario) -> tuple[str, str]:
    request = scenario.executor.request
    author = request.gitea_username or request.ember_user_id
    if not author:
        scenario.fail("Gitea author not configured for this run")
    return author, author.lower()


def _gitea_client(scenario: Scenario, repo: str | GiteaRepository | None = None) -> GiteaClient:
    request = scenario.executor.request
    base_url = request.gitea_base_url
    token = request.gitea_token
    if not base_url or not token:
        scenario.fail("Gitea access is not configured for this eval run")
    default_repo = request.gitea_repo
    repository = GiteaRepository.parse(default_repo) if default_repo else None
    if repo is None and repository is None:
        scenario.fail("No default Gitea repository configured for this eval run")
    client = GiteaClient(base_url=base_url, token=token, default_repo=repository)
    if repo is None:
        return client
    return client.with_repo(repo)


def verify_issue_comment(
    scenario: Scenario,
    *,
    issue: int = 1,
    required_keywords: Iterable[str] | None = None,
    require_checklist: bool = True,
    artifact: str | None = None,
):
    client = _gitea_client(scenario)
    comments = client.issue_comments(issue)
    if not comments:
        scenario.fail(f"No comments found on issue #{issue}")

    expected_display, expected = _expected_author(scenario)
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
        scenario.fail(f"No matching comment from {expected_display}")

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
    client = _gitea_client(scenario, repo)
    branch = client.branch_info(branch_name)
    content = client.file_contents(file, branch.sha)
    if contains not in content:
        scenario.fail(f"{file} on branch {branch_name} missing required content")
    return scenario.ok(description="Verified Gitea branch file", branch=branch_name, commit_sha=branch.sha, file=file)
