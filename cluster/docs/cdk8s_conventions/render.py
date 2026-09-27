"""Render the cdk8s conventions proposal page (`proposal.html`) from `proposal.json`.

`bb run //cluster/docs/cdk8s_conventions:render_bin`, then `pre-commit run --files` on the output
(prettier owns the committed formatting). The page is also published as a claude.ai artifact, whose
publisher wraps it in a document skeleton, so the template emits a fragment without `<html>`/`<head>`.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape
from pydantic import BaseModel, ConfigDict

from util.bazel.workspace import get_build_workspace_directory

_HERE = Path(__file__).parent
_OUTPUT = Path("cluster/docs/cdk8s_conventions/proposal.html")
_SECTIONS = (
    ("answer", "The answer"),
    ("space", "The design space"),
    ("today", "Why it feels wrong"),
    ("needs", "What the layer must do"),
    ("patterns", "Pattern catalog"),
    ("matrix", "Needs × patterns"),
    ("conventions", "The conventions"),
    ("examples", "Worked examples"),
    ("decisions", "Decisions for you"),
    ("prs", "Open PRs"),
    ("migration", "Migration"),
    ("agents-md", "AGENTS.md changes"),
    ("rejected", "Rejected alternatives"),
    ("method", "How this was made"),
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Verdict(StrEnum):
    ADOPT = "adopt"
    RESTRICT = "restrict"
    REJECT = "reject"


class Frequency(StrEnum):
    FREQUENT = "frequent"
    OCCASIONAL = "occasional"
    PLANNED = "planned"
    FORESEEABLE = "foreseeable"


class Disposition(StrEnum):
    LAND = "land as is"
    REVISE = "revise"
    CLOSE = "close"
    FOLD = "fold into migration"
    MERGED = "merged"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    VERIFIED_AFTER_FIXES = "verified after fixes"
    UNVERIFIED = "not verified"


class Pattern(_Model):
    id: str
    name: str
    verdict: Verdict
    reason: str


class DesignOption(_Model):
    name: str
    verdict: Verdict
    note: str


class DesignAxis(_Model):
    axis: str
    question: str
    options: list[DesignOption]
    pick: str


class PriorArt(_Model):
    name: str
    ecosystem: str
    idea: str
    verdict: Verdict
    why: str


class Principle(_Model):
    name: str
    statement: str
    why: str


class Convention(_Model):
    id: str
    name: str
    rule: str
    when_to_use: str
    when_not: str
    signature_shape: str
    patterns_used: list[str]
    replaces: str
    why: str


class MatrixRow(_Model):
    need: str
    need_name: str
    frequency: Frequency
    scores: dict[str, int]
    chosen_conventions: list[str]
    note: str
    today: str


class Verification(_Model):
    status: VerificationStatus
    evidence: str
    fixed: str


class Example(_Model):
    id: str
    title: str
    deploys_to: str
    before: str
    after: str
    rendered_effect: str
    conventions_shown: list[str]
    verification: Verification | None = None


class Decision(_Model):
    question: str
    recommendation: str
    tradeoff: str


class OpenPr(_Model):
    pr: int
    title: str
    recommendation: Disposition
    reason: str


class MigrationStep(_Model):
    step: str
    scope: str
    risk: str


class Rejected(_Model):
    alternative: str
    why_rejected: str


class Score(_Model):
    proposal: str
    feasibility: int
    owner_fit: int
    maintainability: int
    summary: str


class Stat(_Model):
    value: str
    label: str


class CensusBar(_Model):
    mechanism: str
    helpers: int


class Proposal(_Model):
    description: str
    lede: str
    baseline: str
    date: str
    branch: str
    verdict: str
    space_intro: str
    design_space: list[DesignAxis]
    prior_art: list[PriorArt]
    principles: list[Principle]
    diagnosis: list[str]
    stats: list[Stat]
    census: list[CensusBar]
    census_note: str
    one_service: str
    needs_intro: str
    patterns: list[Pattern]
    patterns_intro: str
    mechanics: list[str]
    matrix_intro: str
    matrix: list[MatrixRow]
    conventions: list[Convention]
    examples_intro: str
    examples: list[Example]
    decisions: list[Decision]
    open_prs: list[OpenPr]
    migration: list[MigrationStep]
    agents_md_changes: list[str]
    rejected: list[Rejected]
    method: str
    scorecard: list[Score]

    def patterns_used_by(self, row: MatrixRow) -> set[str]:
        by_id = {c.id: c for c in self.conventions}
        return {p for c in row.chosen_conventions for p in by_id[c].patterns_used}


def inline(text: str) -> Markup:
    """Escape prose, then render its `code` spans and **bold** runs."""
    html = str(escape(text))
    html = re.sub(r"`([^`]+)`", r"<code>\1</code>", html)
    return Markup(re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", html))


def rich(text: str) -> Markup:
    """One paragraph per line; a run of `- ` lines becomes a bullet list."""
    blocks: list[Markup] = []
    items: list[Markup] = []
    for line in (raw.strip() for raw in text.splitlines()):
        if line.startswith("- "):
            items.append(Markup("<li>{}</li>").format(inline(line[2:])))
            continue
        if items:
            blocks.append(Markup("<ul>{}</ul>").format(Markup("").join(items)))
            items = []
        if line:
            blocks.append(Markup("<p>{}</p>").format(inline(line)))
    if items:
        blocks.append(Markup("<ul>{}</ul>").format(Markup("").join(items)))
    return Markup('<div class="rich">{}</div>').format(Markup("").join(blocks))


def render(proposal: Proposal) -> str:
    env = Environment(
        loader=FileSystemLoader(_HERE),
        autoescape=select_autoescape(enabled_extensions=("html", "j2")),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["inline"] = inline
    env.filters["rich"] = rich
    matrix_rows = [
        {
            "title": f"{r.need} · {r.need_name}",
            "today": r.today,
            "note": r.note,
            "conventions": ", ".join(r.chosen_conventions),
        }
        for r in proposal.matrix
    ]
    return env.get_template("proposal.html.j2").render(
        p=proposal,
        sections=_SECTIONS,
        pattern_names={pat.id: pat.name for pat in proposal.patterns},
        css=Markup((_HERE / "page.css").read_text()),
        js=Markup((_HERE / "page.js").read_text()),
        matrix_rows_json=Markup(json.dumps(matrix_rows).replace("</", "<\\/")),
    )


def main() -> None:
    proposal = Proposal.model_validate_json((_HERE / "proposal.json").read_text())
    (get_build_workspace_directory() / _OUTPUT).write_text(render(proposal))


if __name__ == "__main__":
    main()
