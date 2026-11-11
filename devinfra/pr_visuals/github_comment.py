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
COMMENT_OPEN_TARGETS = 2
INLINE_ASSET_LIMIT = 3


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


def _target_section(test: ReviewTest, *, url: str, open_section: bool, ci_details_url: str | None) -> str:
    assets = [
        asset
        for asset in ordered_assets(test.assets)
        if asset.classification in {"modified", "new", "removed", "unstable"}
    ]
    largest_change = max(
        (asset.changed_fraction or 0.0 for asset in assets if asset.classification == "modified"), default=None
    )
    gallery_url = f"{url}tests/{test.slug}/index.html"
    asset_base = f"{url}tests/{test.slug}/"
    invocation_labels, configuration_labels = provenance_labels_for_test(test)
    return str(
        _template().target(
            test,
            largest_change,
            gallery_url,
            ci_details_url,
            [
                _asset_data(
                    asset,
                    test,
                    asset_base=asset_base,
                    gallery_url=gallery_url,
                    invocation_labels=invocation_labels,
                    configuration_labels=configuration_labels,
                )
                for asset in assets
            ],
            open_section,
            INLINE_ASSET_LIMIT,
            invocation_labels,
            configuration_labels,
        )
    )


def _with_target_sections(head: str, review_tests: list[ReviewTest], url: str, *, ci_details_url: str | None) -> str:
    changed = sorted((test for test in review_tests if test.summary and not _is_unchanged(test)), key=test_impact_key)
    unchanged = [test for test in review_tests if _is_unchanged(test)]
    sections = [head, str(_template().changed_heading())]
    omitted = 0
    included = 0
    for test in changed:
        section = _target_section(
            test, url=url, open_section=included < COMMENT_OPEN_TARGETS, ci_details_url=ci_details_url
        )
        if len("\n\n".join([*sections, section])) + 160 > COMMENT_BUDGET:
            omitted += 1
            continue
        sections.append(section)
        included += 1

    if unchanged:
        section = str(_template().unchanged(unchanged, url))
        if len("\n\n".join([*sections, section])) + (160 if omitted else 0) <= COMMENT_BUDGET:
            sections.append(section)
        else:
            sections.append(str(_template().unchanged_omitted(len(unchanged))))
    if omitted:
        sections.append(str(_template().omitted(omitted, url)))
    return "\n\n".join(sections)


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
