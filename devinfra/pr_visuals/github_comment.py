"""Prepare data and render visual-review comments for GitHub."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urljoin

from jinja2 import Environment, FileSystemLoader

from devinfra.pr_visuals.metadata import ReviewAsset, ReviewTest
from devinfra.pr_visuals.render_common import (
    asset_page_data,
    buildbuddy_url,
    classification_totals,
    ordered_assets,
    provenance_labels_for_test,
    shard_ranges,
    test_impact_key,
)

COMMENT_MARKER = "<!-- pr-visuals -->"
COMMENT_METADATA_PREFIX = "<!-- pr-visuals {"
CommentKind = Literal["published", "no_visual", "failure"]


@dataclass(frozen=True)
class CommentState:
    commit_sha: str
    kind: CommentKind
    ci_conclusion: str

    def metadata(self) -> str:
        payload = json.dumps(
            {"version": 1, "commit_sha": self.commit_sha, "kind": self.kind, "ci_conclusion": self.ci_conclusion},
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"<!-- pr-visuals {payload} -->"


COMMENT_BUDGET = 55_000
_COMMENT_CI_CONCLUSIONS = (
    "success",
    "failure",
    "neutral",
    "cancelled",
    "timed_out",
    "action_required",
    "stale",
    "skipped",
    "startup_failure",
)
COMMENT_METADATA_RESERVE = max(
    len(CommentState(commit_sha="0" * 40, kind="published", ci_conclusion=conclusion).metadata()) + 1
    for conclusion in _COMMENT_CI_CONCLUSIONS
)
COMMENT_BODY_BUDGET = COMMENT_BUDGET - COMMENT_METADATA_RESERVE
COMMENT_OPEN_TARGETS = 2
INLINE_ASSET_LIMIT = 3
MAX_COMMENT_TARGET_LABEL_LENGTH = 240
MAX_COMMENT_ASSET_LABEL_LENGTH = 240
MAX_COMMENT_TITLE_LENGTH = 240
MAX_COMMENT_ISSUES = 4
MAX_COMMENT_ISSUE_LENGTH = 500


@lru_cache(maxsize=1)
def _template() -> Any:
    # The only Markdown is the fixed syntax for GitHub's native alerts.
    environment = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=True, trim_blocks=True)
    environment.filters["shard_ranges"] = shard_ranges
    environment.filters["buildbuddy_url"] = buildbuddy_url
    return environment.get_template("github_comment.md.j2").module


def _is_unchanged(test: ReviewTest) -> bool:
    return (
        test.summary is not None
        and not test.issues
        and test.summary.modified == test.summary.new == test.summary.removed == test.summary.unstable == 0
    )


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _comment_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _comment_test(test: ReviewTest) -> ReviewTest:
    return test.model_copy(
        update={
            "target_label": _bounded_text(test.target_label, MAX_COMMENT_TARGET_LABEL_LENGTH),
            "title": _bounded_text(test.title, MAX_COMMENT_TITLE_LENGTH),
            "issues": [
                issue.model_copy(update={"message": _bounded_text(issue.message, MAX_COMMENT_ISSUE_LENGTH)})
                for issue in test.issues[:MAX_COMMENT_ISSUES]
            ],
        }
    )


def _asset_data(
    asset: ReviewAsset,
    test: ReviewTest,
    *,
    asset_base: str,
    gallery_url: str,
    invocation_labels: Mapping[str, str],
    configuration_labels: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "asset": asset,
        "test": test,
        "urls": asset_page_data(
            asset,
            asset_base=asset_base,
            page_url=gallery_url,
            shared_asset_base=urljoin(asset_base, "../../../../"),
            invocation_labels=invocation_labels,
            configuration_labels=configuration_labels,
        ),
        "anchor": f"{gallery_url}#{quote(asset.path, safe='/')}",
    }


def _target_parts(
    test: ReviewTest, *, url: str, open_section: bool, ci_details_url: str | None, assets: list[ReviewAsset]
) -> tuple[str, str, list[tuple[str, str]]]:
    largest_change = max(
        (asset.changed_fraction or 0.0 for asset in assets if asset.classification == "modified"), default=None
    )
    gallery_url = f"{url}tests/{test.slug}/index.html"
    asset_base = f"{url}tests/{test.slug}/"
    display_test = _comment_test(test)
    invocation_labels, configuration_labels = provenance_labels_for_test(display_test)
    template = _template()
    start = str(
        template.target_start(
            display_test,
            largest_change,
            gallery_url,
            ci_details_url,
            max(0, len(test.issues) - MAX_COMMENT_ISSUES),
            open_section,
            invocation_labels,
            configuration_labels,
        )
    )
    end = str(template.target_end())
    item_parts = []
    for asset in assets:
        item = _asset_data(
            asset.model_copy(update={"label": _bounded_text(asset.label, MAX_COMMENT_ASSET_LABEL_LENGTH)}),
            display_test,
            asset_base=asset_base,
            gallery_url=gallery_url,
            invocation_labels=invocation_labels,
            configuration_labels=configuration_labels,
        )
        closed = str(template.target_asset(item, len(assets), False, open_section, INLINE_ASSET_LIMIT))
        opened = str(template.target_asset(item, len(assets), True, open_section, INLINE_ASSET_LIMIT))
        item_parts.append((closed, opened))
    return start, end, item_parts


def _with_target_sections(head: str, review_tests: list[ReviewTest], url: str, *, ci_details_url: str | None) -> str:
    changed = sorted((test for test in review_tests if test.summary and not _is_unchanged(test)), key=test_impact_key)
    unchanged = [_comment_test(test) for test in review_tests if _is_unchanged(test)]
    assets_by_target = [
        [
            asset
            for asset in ordered_assets(test.assets)
            if asset.classification in {"modified", "new", "removed", "unstable"}
        ]
        for test in changed
    ]
    total_assets = sum(map(len, assets_by_target))
    unchanged_sections = [("", False)]
    if unchanged:
        unchanged_sections = [
            (str(_template().unchanged(unchanged, url)), True),
            (str(_template().unchanged_omitted(len(unchanged), f"{url}index.html")), False),
        ]

    template = _template()
    heading = str(template.changed_heading())
    target_parts = [
        _target_parts(
            test,
            url=url,
            open_section=index < COMMENT_OPEN_TARGETS,
            ci_details_url=ci_details_url,
            assets=assets_by_target[index],
        )
        for index, test in enumerate(changed)
    ]

    def make_body(selected: list[set[int]], shown_targets: int, unchanged_section: str) -> str:
        selected_count = sum(map(len, selected))
        omitted_assets = total_assets - selected_count
        omitted_targets = len(changed) - shown_targets
        sections = [head, heading]
        for index in range(shown_targets):
            start, end, item_parts = target_parts[index]
            selected_indices = sorted(selected[index])
            snippets = [
                item_parts[item_index][1 if item_index == selected_indices[0] and index < COMMENT_OPEN_TARGETS else 0]
                for item_index in selected_indices
            ]
            sections.append("\n\n".join([start, *snippets, end]))
        if unchanged_section:
            sections.append(unchanged_section)
        if omitted_assets or omitted_targets:
            sections.append(str(template.omitted(omitted_assets, omitted_targets, f"{url}index.html")))
        return "\n\n".join(sections)

    def body_length(
        selected_counts: list[int],
        selected_lengths: list[int],
        first_selected: list[int | None],
        shown_targets: int,
        unchanged_section: str,
    ) -> int:
        selected_count = sum(selected_counts[:shown_targets])
        omitted_assets = total_assets - selected_count
        omitted_targets = len(changed) - shown_targets
        sections = 2 + shown_targets + bool(unchanged_section) + bool(omitted_assets or omitted_targets)
        result = (
            _comment_length(head) + _comment_length(heading) + (sections - 1) * 2 + _comment_length(unchanged_section)
        )
        if omitted_assets or omitted_targets:
            result += _comment_length(str(template.omitted(omitted_assets, omitted_targets, f"{url}index.html")))
        for index in range(shown_targets):
            start, end, item_parts = target_parts[index]
            result += (
                _comment_length(start)
                + _comment_length(end)
                + 2 * (selected_counts[index] + 1)
                + selected_lengths[index]
            )
            first = first_selected[index]
            if first is not None and index < COMMENT_OPEN_TARGETS:
                result += _comment_length(item_parts[first][1]) - _comment_length(item_parts[first][0])
        return result

    def allocate(unchanged_section: str, detailed_unchanged: bool) -> tuple[str, int, int, bool]:
        selected = [set() for _ in changed]
        selected_counts = [0 for _ in changed]
        selected_lengths = [0 for _ in changed]
        first_selected: list[int | None] = [None for _ in changed]
        shown_targets = len(changed)
        while (
            body_length(selected_counts, selected_lengths, first_selected, shown_targets, unchanged_section)
            > COMMENT_BODY_BUDGET
            and shown_targets
        ):
            shown_targets -= 1
        if (
            body_length(selected_counts, selected_lengths, first_selected, shown_targets, unchanged_section)
            > COMMENT_BODY_BUDGET
        ):
            return make_body(selected, shown_targets, unchanged_section), 0, shown_targets, detailed_unchanged

        # Give each target its next screenshot in impact order before giving any target a second.
        max_assets = max((len(items) for items in assets_by_target[:shown_targets]), default=0)
        selected_count = 0
        for asset_index in range(max_assets):
            for target_index in range(shown_targets):
                item_parts = target_parts[target_index][2]
                if asset_index >= len(item_parts):
                    continue
                selected[target_index].add(asset_index)
                selected_counts[target_index] += 1
                selected_lengths[target_index] += _comment_length(item_parts[asset_index][0])
                if first_selected[target_index] is None:
                    first_selected[target_index] = asset_index
                candidate_length = body_length(
                    selected_counts, selected_lengths, first_selected, shown_targets, unchanged_section
                )
                if candidate_length <= COMMENT_BODY_BUDGET:
                    selected_count += 1
                else:
                    selected[target_index].remove(asset_index)
                    selected_counts[target_index] -= 1
                    selected_lengths[target_index] -= _comment_length(item_parts[asset_index][0])
                    if first_selected[target_index] == asset_index:
                        first_selected[target_index] = min(selected[target_index], default=None)
        body = make_body(selected, shown_targets, unchanged_section)
        return body, selected_count, shown_targets, detailed_unchanged

    choices = [allocate(section, detailed) for section, detailed in unchanged_sections]
    body, _, _, _ = max(
        choices, key=lambda choice: (_comment_length(choice[0]) <= COMMENT_BODY_BUDGET, choice[2], choice[1], choice[3])
    )
    return body


def ci_failure_summary(ci_conclusion: str, ci_failures: list[str]) -> str:
    if ci_conclusion == "success":
        return ""
    if ci_failures:
        return f" Bazel CI concluded {ci_conclusion}; {len(ci_failures)} target(s) failed."
    return f" Bazel CI concluded {ci_conclusion}; failed target details were unavailable."


def _header(
    *,
    repository: str,
    commit_sha: str,
    ci_conclusion: str,
    ci_failures: list[str],
    ci_details_url: str | None = None,
    failed: bool = False,
    review_tests: list[ReviewTest] | None = None,
) -> list[str]:
    template = _template()
    parts = [str(template.header(repository, commit_sha, failed))]
    if ci_details_url:
        parts.append(str(template.rerun(ci_details_url)))
    if ci_conclusion != "success":
        parts.append(str(template.ci_failure(ci_conclusion, ci_failures)))
    fallbacks = [test for test in (review_tests or []) if test.baseline_fallback]
    if fallbacks:
        baselines = sorted({test.base_sha[:8] for test in fallbacks if test.base_sha})
        parts.append(str(template.fallback(len(fallbacks), baselines)))
    return parts


def success_comment_body(
    *,
    repository: str,
    commit_sha: str,
    url: str,
    review_tests: list[ReviewTest],
    base_sha: str | None = None,
    ci_conclusion: str = "success",
    ci_failures: list[str] | None = None,
    ci_details_url: str | None = None,
) -> str:
    template = _template()
    parts = _header(
        repository=repository,
        commit_sha=commit_sha,
        ci_conclusion=ci_conclusion,
        ci_failures=ci_failures or [],
        ci_details_url=ci_details_url,
        review_tests=review_tests,
    )
    page_url = f"{url}index.html"
    if base_sha is None:
        parts.append(str(template.no_base(len(review_tests), page_url, url, review_tests)))
        return "\n\n".join(parts)

    totals = classification_totals(review_tests)
    if totals.modified == totals.new == totals.removed == totals.unstable == 0 and not any(
        t.issues for t in review_tests
    ):
        parts.append(str(template.unchanged_intro(len(review_tests), page_url)))
        return "\n\n".join(parts)
    parts.append(str(template.changed_intro(len(review_tests), page_url, totals)))
    return _with_target_sections("\n\n".join(parts), review_tests, url, ci_details_url=ci_details_url)


def no_visual_comment_body(
    *, repository: str, commit_sha: str, ci_conclusion: str, ci_failures: list[str], details_url: str | None
) -> str:
    parts = _header(repository=repository, commit_sha=commit_sha, ci_conclusion=ci_conclusion, ci_failures=ci_failures)
    parts.append(str(_template().no_visual(details_url)))
    return "\n\n".join(parts)


def error_comment_body(
    *,
    repository: str,
    commit_sha: str,
    error: Exception,
    ci_conclusion: str = "success",
    ci_failures: list[str] | None = None,
    ci_details_url: str | None = None,
) -> str:
    parts = _header(
        repository=repository,
        commit_sha=commit_sha,
        ci_conclusion=ci_conclusion,
        ci_failures=ci_failures or [],
        ci_details_url=ci_details_url,
        failed=True,
    )
    message = str(error)[:2000]
    parts.append(str(_template().error(message)))
    return "\n\n".join(parts)
