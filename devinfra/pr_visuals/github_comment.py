"""Format visual-review comments for GitHub."""

import html
import urllib.parse

from devinfra.pr_visuals.metadata import ClassificationCounts, ReviewAsset, ReviewIssue, ReviewTest
from devinfra.pr_visuals.render_common import (
    _asset_page_data,
    _buildbuddy_url,
    _ordered_assets,
    _shard_ranges,
    _test_impact_key,
    _totals,
)

COMMENT_MARKER = "<!-- pr-visuals -->"
COMMENT_BUDGET = 55_000
COMMENT_OPEN_TARGETS = 2
INLINE_ASSET_LIMIT = 3


def _issue_comment_html(issue: ReviewIssue) -> str:
    message = html.escape(issue.message)
    source = issue.source
    if source is None:
        return message
    shard_label = "shard" if len(source.shards) == 1 else "shards"
    details = f"{shard_label} {_shard_ranges(source.shards)} · run {source.run or 'unknown'} · attempt {source.attempt or 'unknown'}"
    if source.configuration:
        details += f" · config {source.configuration[:8]}"
    url = _buildbuddy_url(source.invocation)
    return f'{message} ({html.escape(details)}) · <a href="{html.escape(url, quote=True)}">BuildBuddy</a>'


def _preview_img(url: str, alt: str) -> str:
    escaped_url = html.escape(url, quote=True)
    escaped_alt = html.escape(alt, quote=True)
    return f'<a href="{escaped_url}"><img src="{escaped_url}" width="260" alt="{escaped_alt}"></a>'


def stale_baseline_note(test: ReviewTest) -> str:
    """Where a stale baseline came from relative to the merge base, in one phrase."""
    baseline = f"`{test.base_sha[:8]}`" if test.base_sha else "an unavailable commit"
    merge_base = f"`{test.merge_base_sha[:8]}`" if test.merge_base_sha else "the merge base"
    return f"{baseline}, not merge base {merge_base}"


def _asset_comment_lines(
    asset: ReviewAsset, *, asset_base: str, gallery_url: str, test: ReviewTest, include_heading: bool = True
) -> list[str]:
    urls = _asset_page_data(
        asset,
        asset_base=asset_base,
        page_url=gallery_url,
        shared_asset_base=urllib.parse.urljoin(asset_base, "../../../../"),
    )
    anchor = html.escape(f"{gallery_url}#{urllib.parse.quote(asset.path, safe='/')}", quote=True)
    label = f'<a href="{anchor}">{html.escape(asset.label)}</a>'
    stale = f" · ⚠️ stale baseline ({stale_baseline_note(test)})" if asset.baseline_stale else ""
    stale_heading = [f"<p>⚠️ stale baseline ({stale_baseline_note(test)})</p>", ""] if asset.baseline_stale else []
    if asset.classification == "unstable":
        heading = [f"<p><strong>{label}</strong> · unstable across runs</p>", ""] if include_heading else []
        return [*heading, *(f"- {_preview_img(v['url'], asset.label)}" for v in urls["variants"])]
    if asset.classification == "modified":
        fraction = asset.changed_fraction or 0.0
        diff = (
            "_(dimensions changed)_"
            if asset.dimension_changed
            else _preview_img(urls["diff_url"], f"{asset.label} diff")
        )
        heading = (
            [f"<p><strong>{label}</strong> · {fraction:.1%} changed{stale}</p>", ""]
            if include_heading
            else stale_heading
        )
        return [
            *heading,
            "| Before | After | Diff |",
            "| --- | --- | --- |",
            f"| {_preview_img(urls['baseline_url'], f'{asset.label} baseline')} "
            f"| {_preview_img(urls['candidate_url'], f'{asset.label} candidate')} "
            f"| {diff} |",
        ]
    if asset.classification == "new":
        heading = [f"<p><strong>{label}</strong> · new</p>", ""] if include_heading else stale_heading
        return [*heading, _preview_img(urls["candidate_url"], asset.label)]
    if asset.classification == "removed":
        heading = [f"<p><strong>{label}</strong> · removed{stale}</p>", ""] if include_heading else stale_heading
        return [*heading, _preview_img(urls["baseline_url"], f"{asset.label} baseline")]
    return []


def _is_unchanged(test: ReviewTest) -> bool:
    return (
        test.summary is not None
        and not test.issues
        and test.summary.modified == test.summary.new == test.summary.removed == test.summary.unstable == 0
    )


def _target_section(test: ReviewTest, *, url: str, open_section: bool, ci_details_url: str | None) -> list[str]:
    counts = test.summary or ClassificationCounts()
    assets = [
        asset
        for asset in _ordered_assets(test.assets)
        if asset.classification in {"modified", "new", "removed", "unstable"}
    ]
    largest_change = max(
        (asset.changed_fraction or 0.0 for asset in assets if asset.classification == "modified"), default=None
    )
    impact = f" · largest change {largest_change:.1%}" if largest_change is not None else ""
    details = "<details open>" if open_section else "<details>"
    gallery_url = f"{url}tests/{test.slug}/index.html"
    asset_base = f"{url}tests/{test.slug}/"
    gallery_link = f'<a href="{html.escape(gallery_url, quote=True)}">link</a>'
    rerun_link = f' · <a href="{html.escape(ci_details_url, quote=True)}">request rerun</a>' if ci_details_url else ""
    counts_text = _format_test_counts(counts)
    if test.issues:
        counts_text = "incomplete or non-comparable" + (f"; {counts_text}" if counts_text != "unchanged" else "")
    lines = [
        details,
        f"<summary><code>{html.escape(test.target_label)}</code> — {counts_text}{impact} · "
        f"{html.escape(test.title)} · {gallery_link}{rerun_link}</summary>",
        "",
    ]
    lines.extend(f"- ⚠️ {_issue_comment_html(issue)}" for issue in test.issues)
    for index, asset in enumerate(assets):
        if len(assets) <= INLINE_ASSET_LIMIT:
            content = _asset_comment_lines(asset, asset_base=asset_base, gallery_url=gallery_url, test=test)
            lines.extend([*content, ""])
            continue
        asset_title = html.escape(asset.label)
        if asset.classification == "modified" and asset.changed_fraction is not None:
            asset_title += f" — {asset.changed_fraction:.1%} changed"
        elif asset.classification is not None:
            asset_title += f" — {asset.classification}"
        if asset.baseline_stale:
            asset_title += " — ⚠️ stale baseline"
        asset_anchor = html.escape(f"{gallery_url}#{urllib.parse.quote(asset.path, safe='/')}", quote=True)
        # Only a short link: clicking elsewhere in the summary toggles the disclosure.
        asset_title += f' · <a href="{asset_anchor}">link</a>'
        content = _asset_comment_lines(
            asset, asset_base=asset_base, gallery_url=gallery_url, test=test, include_heading=False
        )
        asset_open = open_section and index == 0
        lines += [
            "<details open>" if asset_open else "<details>",
            f"<summary>{asset_title}</summary>",
            "",
            *content,
            "",
            "</details>",
            "",
        ]
    lines.append("</details>")
    return lines


def _unchanged_section(review_tests: list[ReviewTest], *, url: str) -> list[str]:
    unchanged = [test for test in review_tests if _is_unchanged(test)]
    if not unchanged:
        return []
    plural = "" if len(unchanged) == 1 else "s"
    return [
        "<details>",
        f"<summary>{len(unchanged)} unchanged target{plural}</summary>",
        "",
        *(f"- [`{test.target_label}`]({url}tests/{test.slug}/index.html)" for test in unchanged),
        "",
        "</details>",
    ]


def _with_target_sections(
    head: list[str], review_tests: list[ReviewTest], url: str, *, ci_details_url: str | None
) -> str:
    changed = [test for test in review_tests if test.summary and not _is_unchanged(test)]
    changed.sort(key=_test_impact_key)
    unchanged = _unchanged_section(review_tests, url=url)
    lines = [*head, "### Changed tests", ""]
    omitted = 0
    included = 0
    for test in changed:
        section = _target_section(
            test, url=url, open_section=included < COMMENT_OPEN_TARGETS, ci_details_url=ci_details_url
        )
        candidate = "\n".join([*lines, *section])
        if len(candidate) + 160 > COMMENT_BUDGET:
            omitted += 1
            continue
        lines.extend(section)
        included += 1

    if unchanged:
        candidate = "\n".join([*lines, *unchanged])
        if len(candidate) + (160 if omitted else 0) <= COMMENT_BUDGET:
            lines.extend(unchanged)
        else:
            unchanged_count = sum(1 for test in review_tests if _is_unchanged(test))
            lines += ["", f"{unchanged_count} unchanged targets; see the full gallery."]

    if omitted:
        lines += [
            "",
            f"{omitted} changed test section{'' if omitted == 1 else 's'} omitted for length. "
            f"[Full gallery]({url}index.html).",
        ]
    return "\n".join(lines)


def _format_test_counts(counts: ClassificationCounts) -> str:
    """`"4 modified, 12 new"` — only the non-zero buckets, so an untouched target's bullet line
    doesn't read `"0 modified, 0 new, 0 removed"`. `"unchanged"` when every bucket is zero."""
    parts = [
        f"{count} {label}"
        for count, label in (
            (counts.unstable, "unstable"),
            (counts.modified, "modified"),
            (counts.new, "new"),
            (counts.removed, "removed"),
        )
        if count
    ]
    return ", ".join(parts) if parts else "unchanged"


def _ci_failure_notice(ci_conclusion: str, ci_failures: list[str]) -> list[str]:
    if ci_conclusion == "success":
        return []
    lines = ["> [!WARNING]", f"> Bazel CI concluded `{ci_conclusion}`; showing the visual artifacts that arrived."]
    if ci_failures:
        lines += [">", "> Failed Bazel targets:"]
        displayed = ci_failures[:20]
        lines.extend(f"> - `{target}`" for target in displayed)
        if len(ci_failures) > len(displayed):
            lines.append(f"> - … and {len(ci_failures) - len(displayed)} more")
    else:
        lines.append("> Failed target labels unavailable from BuildBuddy.")
    return lines


def _ci_failure_summary(ci_conclusion: str, ci_failures: list[str]) -> str:
    if ci_conclusion == "success":
        return ""
    if ci_failures:
        return f" Bazel CI concluded {ci_conclusion}; {len(ci_failures)} target(s) failed."
    return f" Bazel CI concluded {ci_conclusion}; failed target details were unavailable."


def _fallback_baseline_notice(review_tests: list[ReviewTest]) -> list[str]:
    """Explain when a PR comparison could not use its exact base commit.

    A per-target devel pointer is useful for showing a newly introduced visual
    target, but it can lag the PR base.  Its differences must not be presented
    as changes attributable solely to the PR.
    """
    fallbacks = [test for test in review_tests if test.baseline_fallback]
    if not fallbacks:
        return []
    baselines = sorted({test.base_sha[:8] for test in fallbacks if test.base_sha})
    target_count = len(fallbacks)
    target_word = "target" if target_count == 1 else "targets"
    baseline_word = "baseline" if len(baselines) == 1 else "baselines"
    baseline_list = ", ".join(f"`{baseline}`" for baseline in baselines) or "an unavailable commit"
    return [
        "> [!WARNING]",
        f"> {target_count} {target_word} used the latest devel {baseline_word} ({baseline_list}) because the "
        "PR base published none. Diffs there may include devel drift, not just this PR's changes.",
    ]


def _ci_rerun_notice(details_url: str | None) -> list[str]:
    if details_url is None:
        return []
    return [f"Request a manual rerun from the [CI run page]({details_url})."]


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
    ci_failures = ci_failures or []
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    page_url = f"{url}index.html"
    target_count = len(review_tests)
    plural = "" if target_count == 1 else "s"
    lines = [COMMENT_MARKER, f"## Visual review for [`{commit_sha[:8]}`]({commit_url})", ""]
    if notice := _ci_rerun_notice(ci_details_url):
        lines += [*notice, ""]
    if notice := _ci_failure_notice(ci_conclusion, ci_failures):
        lines += [*notice, ""]
    if notice := _fallback_baseline_notice(review_tests):
        lines += [*notice, ""]
    if base_sha is None:
        lines += [
            f"{target_count} Bazel test target{plural} produced visual artifacts. [Open visual review]({page_url}).",
            "",
        ]
        lines.extend(
            f"- [`{test.target_label}`]({url}tests/{test.slug}/index.html): {len(test.assets)} assets"
            for test in review_tests
        )
        return "\n".join(lines)

    totals = _totals(review_tests)
    if totals.modified == totals.new == totals.removed == totals.unstable == 0 and not any(
        t.issues for t in review_tests
    ):
        lines.append(
            f"No visual changes among the {target_count} affected Bazel test target{plural}. "
            f"[Open visual review]({page_url})."
        )
        return "\n".join(lines)

    lines += [
        f"{target_count} Bazel test target{plural} produced visual artifacts · "
        f"**{totals.modified} modified**, {totals.new} new, {totals.removed} removed, "
        f"{totals.unchanged} unchanged, {totals.unstable} unstable. [Open visual review]({page_url}).",
        "",
    ]
    return _with_target_sections(lines, review_tests, url, ci_details_url=ci_details_url)


def no_visual_comment_body(
    *, repository: str, commit_sha: str, ci_conclusion: str, ci_failures: list[str], details_url: str | None
) -> str:
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    lines = [COMMENT_MARKER, f"## Visual review for [`{commit_sha[:8]}`]({commit_url})", ""]
    if notice := _ci_failure_notice(ci_conclusion, ci_failures):
        lines += [*notice, ""]
    lines.append("No visual artifacts were available from the Bazel CI run.")
    if details_url:
        lines.append(f"[Open the CI run]({details_url}) for the complete test results or to request a manual rerun.")
    return "\n".join(lines)


def error_comment_body(
    *,
    repository: str,
    commit_sha: str,
    error: Exception,
    ci_conclusion: str = "success",
    ci_failures: list[str] | None = None,
    ci_details_url: str | None = None,
) -> str:
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    message = str(error).replace("```", "'''")[:2000]
    lines = [COMMENT_MARKER, f"## Visual review failed for [`{commit_sha[:8]}`]({commit_url})", ""]
    if notice := _ci_rerun_notice(ci_details_url):
        lines += [*notice, ""]
    if notice := _ci_failure_notice(ci_conclusion, ci_failures or []):
        lines += [*notice, ""]
    lines += [
        "> [!CAUTION]",
        "> The visual-review publisher failed while processing this Bazel CI run.",
        "",
        "```text",
        message,
        "```",
    ]
    return "\n".join(lines)
