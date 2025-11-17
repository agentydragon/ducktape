import os
import shutil

import pytest

from adgn.openai_utils.client_factory import build_client
from adgn.props.lint_issue import lint_issue_run
from adgn.props.models.issue import IssueCore, LineRange, Occurrence


@pytest.mark.live_llm
# XFAIL: specimen acquisition currently depends on local GitHub credentials. TODO: make credential-free.
@pytest.mark.xfail(
    reason="Specimen acquisition for 2025-09-02-ducktape_wt depends on local GitHub creds; switch to token/codeload or vendored LocalSource.",
    strict=False,
    run=False,
)
@pytest.mark.parametrize(
    ("initial_range", "allowed_window", "entity"),
    [
        # StatusSnapshot: initial [413, 424] -> allowed start [410..412], end [421..423]
        ((413, 424), ((410, 412), (421, 423)), "StatusSnapshot"),
        # WorktreeRuntime: initial [425, 429] -> allowed start [422..424], end [427..429]
        ((425, 429), ((422, 424), (427, 429)), "WorktreeRuntime"),
        # GitStatusdProcess: initial [640, 1144] -> allowed start [638..640], end [1142..1144]
        ((640, 1144), ((638, 640), (1142, 1144)), "GitStatusdProcess"),
        # _record_github_error: initial [1130, 1233] -> allowed start [1129..1130], end [1232..1233]
        ((1130, 1233), ((1129, 1130), (1232, 1233)), "_record_github_error"),
    ],
)
async def test_iss014_anchor_windows(
    initial_range: tuple[int, int], allowed_window: tuple[tuple[int, int], tuple[int, int]], entity: str
):
    """Runs the lint-issue agent for iss-014 on the wt specimen per occurrence and
    asserts the corrected anchors fall within allowed inclusive windows.

    This is a live LLM integration test; skipped unless OPENAI_API_KEY is set and Docker is available.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")

    specimen = "2025-09-02-ducktape_wt"
    path = "wt/wt/server/wt_server.py"

    # Build injected IssueCore + Occurrence from the provided spec
    issue_core = IssueCore(
        id="iss-014",
        should_flag=True,
        rationale="Delete StatusSnapshot - dead code; never used and should be removed.",
        # properties=["no-dead-code"],  # deprecated; omit
    )
    s, e = initial_range
    occ = Occurrence(files={path: [LineRange(start_line=s, end_line=e)]}, note=entity)

    payload = await lint_issue_run(
        specimen=specimen,
        issue_core=issue_core,
        occurrence=occ,
        model="gpt-5",
        gitconfig=None,
        client=build_client("gpt-5"),
    )

    # Extract corrected anchors from AnchorIncorrect findings
    ca: dict[str, list[LineRange]] = {}
    for finding_record in payload.findings:
        if finding_record.finding.kind == "ANCHOR_INCORRECT":
            file = finding_record.finding.correction.file
            range_obj = finding_record.finding.correction.range
            if file not in ca:
                ca[file] = []
            ca[file].append(range_obj)

    # Effective ranges: if agent omitted corrections (None) or file entry is None, treat as unchanged
    s, e = initial_range
    if ca is None or ca.get(path) is None:
        effective = [(s, e)]
    else:
        # Assert no unrelated non-null files were returned
        non_null_paths = {p for p, rs in ca.items() if rs is not None}
        assert non_null_paths <= {path}, f"Unexpected paths with ranges in corrected_anchors: {non_null_paths}"
        effective = [(r.start_line, r.end_line) for r in (ca.get(path) or []) if r.end_line is not None]

    assert len(effective) == 1, f"Expected exactly one effective range for {path}, got: {effective}"
    estart, eend = effective[0]
    assert eend is not None, "Expected a closed interval (start,end), not a single-line anchor"

    (smin, smax), (emin, emax) = allowed_window
    assert smin <= estart <= smax, f"start_line {estart} outside allowed [{smin}..{smax}] for entity {entity}"
    assert emin <= eend <= emax, f"end_line {eend} outside allowed [{emin}..{emax}] for entity {entity}"
