from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from hamcrest import assert_that, has_item
from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.table import Table

from adgn.agent.agent_progress import OneLineProgressHandler
from adgn.llm.rendering.rich_renderers import render_to_rich
from adgn.openai_utils.model import (
    FunctionCallItem,
    FunctionToolParam,
    OpenAIModelProto,
    ResponsesRequest,
    ToolChoiceFunction,
    UserMessage,
)
from adgn.props.models.issue import IssueCore, IssueId, LineRange, Occurrence
from adgn.props.models.lint import AnchorIncorrect, PropertyIncorrectlyAssigned, PropertyShouldBeAssigned

from .lint_issue import lint_issue_run


# ---------- Expectations / Assertions ----------
class AnchorExpectation(BaseModel):
    kind: Literal["anchor"] = "anchor"
    start_window: tuple[int, int] = Field(..., description="Allowed start window [smin, smax]")
    end_window: tuple[int, int] = Field(..., description="Allowed end window [emin, emax]")


class RationaleExpectation(BaseModel):
    kind: Literal["rationale"] = "rationale"
    rubric: str = Field(
        ...,
        description=(
            "Instruction text explaining how the rationale should be corrected; phrase as a positive question where YES means correct behavior"
        ),
    )


class PropertyFindingExpectation(BaseModel):
    kind: Literal["property_finding"] = "property_finding"
    finding: Literal["PROPERTY_INCORRECTLY_ASSIGNED", "PROPERTY_SHOULD_BE_ASSIGNED"]
    property: str = Field(..., description="Property id expected in the finding payload")


class FindingsMatcherExpectation(BaseModel):
    kind: Literal["findings_matcher"] = "findings_matcher"
    matcher: Any


Expectation = Annotated[
    AnchorExpectation | RationaleExpectation | PropertyFindingExpectation | FindingsMatcherExpectation,
    Field(discriminator="kind"),
]


class GradeRationaleArgs(BaseModel):
    verdict: Literal["YES", "PARTIALLY", "NO"]
    reason: str
    model_config = ConfigDict(extra="forbid")


class OccurrenceCase(BaseModel):
    """One occurrence and its expectations."""

    occurrence: Occurrence
    expectations: list[Expectation] | None = Field(default=None, description="Expectations for this occurrence")


class IssueEvalSpec(BaseModel):
    """One issue under a specimen with multiple occurrence cases."""

    specimen: str
    issue: IssueCore
    cases: list[OccurrenceCase]


# Canonical dataset: 2025-09-02-ducktape_wt iss-014 (four occurrences)
WT_ISS014_CASES: list[OccurrenceCase] = [
    OccurrenceCase(
        occurrence=Occurrence(
            files={"wt/wt/server/wt_server.py": [LineRange(start_line=413, end_line=424)]}, note="StatusSnapshot"
        ),
        expectations=[AnchorExpectation(start_window=(410, 412), end_window=(421, 423))],
    ),
    OccurrenceCase(
        occurrence=Occurrence(
            files={"wt/wt/server/wt_server.py": [LineRange(start_line=425, end_line=429)]}, note="WorktreeRuntime"
        ),
        expectations=[AnchorExpectation(start_window=(422, 424), end_window=(427, 429))],
    ),
    OccurrenceCase(
        occurrence=Occurrence(
            files={"wt/wt/server/wt_server.py": [LineRange(start_line=640, end_line=1144)]}, note="GitStatusdProcess"
        ),
        expectations=[AnchorExpectation(start_window=(638, 640), end_window=(1142, 1144))],
    ),
    OccurrenceCase(
        occurrence=Occurrence(
            files={"wt/wt/server/wt_server.py": [LineRange(start_line=1130, end_line=1233)]},
            note="_record_github_error",
        ),
        expectations=[AnchorExpectation(start_window=(1129, 1130), end_window=(1232, 1233))],
    ),
]


class SampleRunSummary(BaseModel):
    specimen: str
    issue_id: IssueId
    total: int
    passed: int
    failed: int
    summary_path: str


class SampleIndexEntry(BaseModel):
    name: str
    specimen: str
    issue_id: IssueId
    summary: str
    total: int
    passed: int
    failed: int


class EvalIndex(BaseModel):
    samples: list[SampleIndexEntry]


async def _grade_rationale_with_llm(
    client: OpenAIModelProto, original: str, proposed: str, *, rubric: str, model: str = "gpt-5"
) -> dict[str, str]:
    """Force a tool call that returns verdict: YES | PARTIALLY | NO, with reason."""
    if not proposed or not proposed.strip():
        return {"verdict": "NO", "reason": "No suggested rationale provided by linter."}
    tools: list[FunctionToolParam] = [
        FunctionToolParam(
            name="grade_rationale",
            description="Return verdict and brief reason.",
            parameters=GradeRationaleArgs.model_json_schema(),
            strict=True,
        )
    ]
    prompt = (
        "Original issue description:\n"
        + original.strip()
        + "\n\nNew issue description:\n"
        + proposed.strip()
        + "\n\n"
        + rubric.strip()
        + "\n\nQuestion: Is the new description corrected as it should be?"
    )
    req = ResponsesRequest(
        input=[UserMessage.text(prompt)], tools=tools, tool_choice=ToolChoiceFunction(name="grade_rationale")
    )
    resp = await client.responses_create(req)
    # Extract function call robustly; fail fast on missing/invalid
    call: FunctionCallItem | None = next(
        (it for it in resp.output if isinstance(it, FunctionCallItem) and it.name == "grade_rationale"), None
    )
    if call is None:
        raise RuntimeError("grade_rationale function call not returned by model")

    raw_args = call.arguments
    if raw_args is None:
        parsed_args: GradeRationaleArgs = GradeRationaleArgs(verdict="NO", reason="")
    else:
        if isinstance(raw_args, str):
            try:
                loaded = json.loads(raw_args)
            except Exception as e:  # pragma: no cover - defensive error surfacing
                raise RuntimeError("grade_rationale arguments not valid JSON") from e
        elif isinstance(raw_args, dict):
            loaded = raw_args
        else:
            raise RuntimeError(f"grade_rationale arguments unsupported type: {type(raw_args).__name__}")

        try:
            parsed_args = GradeRationaleArgs.model_validate(loaded)
        except Exception as e:
            raise RuntimeError("grade_rationale payload failed validation") from e

    verdict = parsed_args.verdict
    reason = parsed_args.reason.strip()
    if verdict not in ("YES", "PARTIALLY", "NO"):
        raise RuntimeError("grade_rationale returned unexpected verdict")
    return {"verdict": verdict, "reason": reason}


async def eval_issue_spec(
    spec: IssueEvalSpec,
    *,
    model: str = "gpt-5",
    gitconfig: str | None = None,
    client: OpenAIModelProto,
    out_dir: Path | str | None = None,
    id_prefix: str = "",
) -> SampleRunSummary:
    """Run lint_issue_run over a list of cases and write an eval summary.

    Returns a structured SampleRunSummary and writes summary.json to out_dir.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Allow local renamings/prefix for clustering or grading runs
    effective_issue = (
        spec.issue
        if not id_prefix
        else IssueCore(
            id=f"{id_prefix}{spec.issue.id}",
            should_flag=spec.issue.should_flag,
            rationale=spec.issue.rationale,
            gap_note=spec.issue.gap_note,
        )
    )

    base = (
        Path(out_dir)
        if out_dir is not None
        else (Path.cwd() / "runs" / "evals" / f"{spec.specimen}_{effective_issue.id}_{ts}")
    )
    base.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    passes = 0

    for idx, case in enumerate(spec.cases):
        exps_raw = case.expectations
        exps: list[Expectation] = list(exps_raw) if exps_raw is not None else []

        occ = case.occurrence
        files_items = list(occ.files.items())
        if len(files_items) != 1:
            raise SystemExit("Case occurrence must target exactly one file")
        path, ranges = files_items[0]
        # Occurrence.files values are list[LineRange] | None in the schema; enforce exactly one range here.
        if ranges is None:
            raise SystemExit(f"Case occurrence for {path} must have exactly one range (got None)")
        if len(ranges) != 1:
            raise SystemExit(f"Case occurrence for {path} must have exactly one range")
        first = ranges[0]
        start_line = first.start_line
        end_line = first.end_line
        entity = occ.note or ""

        payload = await lint_issue_run(
            specimen=spec.specimen,
            issue_core=effective_issue,
            occurrence=occ,
            model=model,
            gitconfig=gitconfig,
            client=client,
            handlers=[OneLineProgressHandler()],
        )

        # Print the structured output object produced by the agent for this case
        Console().print(f"[bold]{spec.specimen} {effective_issue.id} case {idx} {path}[/bold]")
        Console().print(render_to_rich(payload))

        # Effective ranges: derive corrections from AnchorIncorrect findings when present
        corrections: dict[str, list[LineRange]] = {}
        if payload.findings:
            for fr in payload.findings:
                f = fr.finding
                if isinstance(f, AnchorIncorrect):
                    corr = f.correction
                    corrections.setdefault(corr.file, []).append(corr.range)

        ranges = corrections.get(path, [])
        all_ranges = [(r.start_line, r.end_line) for r in ranges]
        effective: list[tuple[int, int | None]] = all_ranges or [(start_line, end_line)]

        estart, eend = effective[0]

        # Evaluate expectations
        case_pass = True
        exp_results: list[dict[str, Any]] = []
        for exp in exps:
            if isinstance(exp, AnchorExpectation):
                smin, smax = exp.start_window
                emin, emax = exp.end_window
                ok = (eend is not None) and (smin <= estart <= smax) and (emin <= eend <= emax)
                exp_results.append(
                    {
                        "kind": "anchor",
                        "start_window": list(exp.start_window),
                        "end_window": list(exp.end_window),
                        "effective_range": [estart, eend],
                        "passed": bool(ok),
                    }
                )
                case_pass = case_pass and ok
            elif isinstance(exp, RationaleExpectation):
                grade = await _grade_rationale_with_llm(
                    client, spec.issue.rationale, payload.suggested_rationale or "", rubric=exp.rubric, model=model
                )
                ok = grade.get("verdict") == "YES"
                exp_results.append(
                    {
                        "kind": "rationale",
                        "verdict": grade.get("verdict"),
                        "reason": grade.get("reason"),
                        "rubric": exp.rubric,
                        "passed": bool(ok),
                    }
                )
                case_pass = case_pass and ok
            elif isinstance(exp, PropertyFindingExpectation):
                # Verify that the linter emitted the expected property finding
                found = False
                details: list[dict[str, str]] = []
                for fr in payload.findings:
                    f = fr.finding
                    # All variants have kind; only some carry a property field
                    kind = f.kind
                    if isinstance(f, PropertyIncorrectlyAssigned | PropertyShouldBeAssigned):
                        details.append({"kind": str(kind), "property": str(f.property)})
                        if kind == exp.finding and f.property == exp.property:
                            found = True
                exp_results.append(
                    {
                        "kind": "property_finding",
                        "expected": {"finding": exp.finding, "property": exp.property},
                        "observed": details,
                        "passed": bool(found),
                    }
                )
                case_pass = case_pass and found
            elif isinstance(exp, FindingsMatcherExpectation):
                # Apply a PyHamcrest matcher to the REAL finding objects (not flattened dicts)
                try:
                    assert_that(payload.findings, exp.matcher)
                    ok = True
                    reason = None
                except AssertionError as e:
                    ok = False
                    reason = str(e)
                exp_results.append({"kind": "findings_matcher", "passed": bool(ok), "reason": reason})
                case_pass = case_pass and ok

        passes += int(case_pass)

        # Write per-case payload
        (base / f"case_{idx:02d}_payload.json").write_text(payload.model_dump_json(indent=2), encoding="utf-8")

        item: dict[str, Any] = {
            "index": idx,
            "path": path,
            "entity": entity,
            "initial_range": [start_line, end_line],
            "ranges_reported": all_ranges,
            "effective_range": [estart, eend],
            "passed": case_pass,
            "expectations": exp_results,
            "message_excerpt": payload.message_md[:400],
        }
        results.append(item)

    summary_obj = {
        "specimen": spec.specimen,
        "issue_id": effective_issue.id,
        "total": len(spec.cases),
        "passed": passes,
        "failed": len(spec.cases) - passes,
        "results": results,
    }
    (base / "summary.json").write_text(json.dumps(summary_obj, indent=2), encoding="utf-8")

    return SampleRunSummary(
        specimen=spec.specimen,
        issue_id=effective_issue.id,
        total=len(spec.cases),
        passed=passes,
        failed=len(spec.cases) - passes,
        summary_path=str(base / "summary.json"),
    )


# Flat list of samples (no dataset abstraction)
SAMPLES: list[IssueEvalSpec] = [
    IssueEvalSpec(
        specimen="2025-09-02-ducktape_wt",
        issue=IssueCore(
            id="iss-014",
            should_flag=True,
            rationale="Delete StatusSnapshot - dead code; never used and should be removed.",
            # properties=["no-dead-code"],
        ),
        cases=list(WT_ISS014_CASES),
    ),
    IssueEvalSpec(
        specimen="2025-09-02-ducktape_wt",
        issue=IssueCore(
            id="iss-036",
            should_flag=True,
            rationale=(
                "Prefer a single pre-check + list comprehension for simple arg filtering to reduce nesting and eliminate one-off append/continue state."
            ),
            # properties=["minimize-nesting"],
            gap_note=(
                "GAP: Prefer comprehensions for simple filter/map over loops with append/continue when it fits on one readable line."
            ),
        ),
        cases=[
            OccurrenceCase(
                occurrence=Occurrence(
                    files={"wt/wt/cli.py": [LineRange(start_line=143, end_line=143)]},
                    note="arg filtering loop (prefer comprehension)",
                ),
                expectations=[AnchorExpectation(start_window=(138, 143), end_window=(152, 153))],
            )
        ],
    ),
    IssueEvalSpec(
        specimen="2025-09-02-ducktape_wt",
        issue=IssueCore(
            id="iss-046",
            should_flag=True,
            rationale="`parse_gitstatusd_response` is a thin wrapper around GitStatusdProtocol; migrate callers to Protocol methods and delete.",
            # properties=["no-dead-code"],
        ),
        cases=[
            OccurrenceCase(
                occurrence=Occurrence(
                    files={"wt/wt/server/gitstatusd_client.py": [LineRange(start_line=358, end_line=360)]},
                    note="parse_gitstatusd_response",
                ),
                expectations=[
                    AnchorExpectation(start_window=(356, 358), end_window=(360, 362)),
                    RationaleExpectation(
                        rubric="Original says migrate callers; there are no callers. New rationale should simply prescribe deleting dead code without mentioning callers."
                    ),
                ],
            )
        ],
    ),
    IssueEvalSpec(
        specimen="2025-09-02-ducktape_wt",
        issue=IssueCore(
            id="iss-047",
            should_flag=True,
            rationale="`create_gitstatusd_request` is a thin wrapper around GitStatusdProtocol; migrate callers to Protocol methods and delete.",
            # properties=["no-dead-code"],
        ),
        cases=[
            OccurrenceCase(
                occurrence=Occurrence(
                    files={"wt/wt/server/gitstatusd_client.py": [LineRange(start_line=363, end_line=370)]},
                    note="create_gitstatusd_request",
                ),
                expectations=[
                    AnchorExpectation(start_window=(361, 363), end_window=(370, 372)),
                    RationaleExpectation(
                        rubric="Original says migrate callers; there are no callers. New rationale should simply prescribe deleting dead code without mentioning callers."
                    ),
                ],
            )
        ],
    ),
    # New sample: duplication misassigned to no-oneoff...
    IssueEvalSpec(
        specimen="2025-09-02-ducktape_wt",
        issue=IssueCore(
            id="iss-049",
            should_flag=True,
            rationale=(
                "Duplicate `daemon_cleanup` helper defined twice (one copy at 216-222); extract a single helper and reuse."
            ),
            # properties=["no-oneoff-vars-and-trivial-wrappers"],
        ),
        cases=[
            OccurrenceCase(
                occurrence=Occurrence(
                    files={"wt/tests/integration/test_shell_integration.py": [LineRange(start_line=216, end_line=222)]},
                    note="daemon_cleanup duplicate",
                ),
                expectations=[
                    FindingsMatcherExpectation(
                        matcher=has_item(PropertyIncorrectlyAssigned(property="no-oneoff-vars-and-trivial-wrappers"))
                    )
                ],
            )
        ],
    ),
    # New sample: iss-031 should NOT be early-bailout (misassignment)
    IssueEvalSpec(
        specimen="2025-09-03-ducktape-llm",
        issue=IssueCore(
            id="iss-031",
            should_flag=True,
            rationale=(
                "Remove no-op timeout branch (dead code); prefer fail-fast or enforce real timeout. Delete empty branch."
            ),
            # properties=["no-dead-code", "early-bailout"],
        ),
        cases=[
            OccurrenceCase(
                occurrence=Occurrence(
                    files={"adgn/src/adgn/llm/mcp/docker_exec/server.py": [LineRange(start_line=181, end_line=183)]},
                    note="no-op timeout branch",
                ),
                expectations=[
                    FindingsMatcherExpectation(matcher=has_item(PropertyIncorrectlyAssigned(property="early-bailout")))
                ],
            )
        ],
    ),
    # New sample: iss-054 should NOT be marked as no-oneoff-vars-and-trivial-wrappers (misassignment)
    IssueEvalSpec(
        specimen="2025-09-02-ducktape_wt",
        issue=IssueCore(
            id="iss-054",
            should_flag=True,
            rationale=(
                "`format_list_with_more` exposes an unused `max_items` parameter; callers never vary it so the parameter should be removed. Prefer a named constant for the default if needed."
            ),
            # properties=["no-oneoff-vars-and-trivial-wrappers"],
        ),
        cases=[
            OccurrenceCase(
                occurrence=Occurrence(
                    files={"wt/wt/client/view_formatter.py": [LineRange(start_line=37, end_line=37)]},
                    note="format_list_with_more max_items unused",
                ),
                expectations=[
                    FindingsMatcherExpectation(
                        matcher=has_item(PropertyIncorrectlyAssigned(property="no-oneoff-vars-and-trivial-wrappers"))
                    )
                ],
            )
        ],
    ),
]


async def run_all_evals(
    *,
    model: str = "gpt-5",
    gitconfig: str | None = None,
    client: OpenAIModelProto,
    root_out: Path | None = None,
    concurrency: int = 4,
    id_prefix: str = "",
) -> EvalIndex:
    """Run all samples concurrently (bounded), print a Rich summary, and return EvalIndex."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(root_out) if root_out is not None else (Path.cwd() / "runs" / "evals" / f"all_{ts}")
    root.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run_one(sample: IssueEvalSpec) -> SampleIndexEntry:
        async with sem:
            effective_id = f"{id_prefix}{sample.issue.id}" if id_prefix else sample.issue.id
            out_dir = root / effective_id
            summary = await eval_issue_spec(
                spec=sample, model=model, gitconfig=gitconfig, client=client, out_dir=out_dir, id_prefix=id_prefix
            )
            return SampleIndexEntry(
                name=effective_id,
                specimen=sample.specimen,
                issue_id=effective_id,
                summary=summary.summary_path,
                total=summary.total,
                passed=summary.passed,
                failed=summary.failed,
            )

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_run_one(s)) for s in SAMPLES]
    entries = [await t for t in tasks]

    (root / "index.json").write_text((eval_index := EvalIndex(samples=list(entries))).model_dump_json(indent=2), encoding="utf-8")

    # Pretty print a concise Rich table summary (in-memory; no read-back)
    table = Table(title="Eval Summary", show_lines=False)
    table.add_column("Sample", style="bold")
    table.add_column("Specimen")
    table.add_column("Issue")
    table.add_column("Total", justify="right")
    table.add_column("Passed", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")

    for ent in eval_index.samples:
        table.add_row(ent.name, ent.specimen, ent.issue_id, str(ent.total), str(ent.passed), str(ent.failed))
    Console().print(table)

    return eval_index
