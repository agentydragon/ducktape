"""Pydantic models for YAML issue file parsing.

⚠️⚠️⚠️ PRIVATE MODULE - DO NOT IMPORT OUTSIDE db/sync/ ⚠️⚠️⚠️

These models provide a permissive input layer for YAML parsing with flexible location shapes.
They normalize and validate YAML data, then expand to canonical TruePositive/FalsePositive models.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from props.core.ids import SnapshotSlug
from props.core.models.true_positive import FalsePositiveOccurrence, LineRange, TruePositiveOccurrence
from props.core.models.types import Rationale
from props.db.sync._models import FalsePositive, TruePositive


class YAMLOccurrence(BaseModel):
    """Permissive input model for YAML occurrences - accepts multiple location shapes.

    Supports flexible line specifications:
    - Line range: [10, 20] → normalized to [LineRange(10, 20)]
    - Multiple ranges: [[10, 15], [20, 25]] → normalized to [LineRange(10, 15), LineRange(20, 25)]
    - Dict with note: {start_line: 42, end_line: 42, note: "..."} → [LineRange(42, 42, "...")]
    - No specific lines: null → kept as None

    After field validation, files dict contains list[LineRange] or None values.
    """

    occurrence_id: str = Field(description="Unique ID within issue (e.g., 'occ-0', 'occ-1')")
    # Type annotation is post-validation (normalize_files converts flexible input to canonical form)
    files: dict[str, list[LineRange] | None] = Field(
        description="File paths to line specifications (normalized to list of LineRange objects with optional notes)"
    )
    note: str | None = Field(default=None, description="Occurrence-specific explanation")
    critic_scopes_expected_to_recall: list[list[str]] | None = Field(
        default=None, description="Critic scope file sets where this counts toward recall (TPs only)"
    )
    relevant_files: list[str] | None = Field(default=None, description="Files making this FP relevant (FPs only)")
    graders_match_only_if_reported_on: list[str] | None = Field(
        default=None,
        description=(
            "GRADING OPTIMIZATION: Restricts which critique outputs can match this occurrence. "
            "If set, a critique reporting issues only in files OUTSIDE this set will be skipped "
            "during matching (assumed non-match without semantic comparison). "
            "NULL = allow matching from any file (conservative default). "
            "Non-empty = skip matching if critique's files don't overlap."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("graders_match_only_if_reported_on", mode="before")
    @classmethod
    def validate_graders_match_only_if_reported_on(cls, v: list[str] | None) -> list[str] | None:
        """Reject empty list - must be null or non-empty."""
        if v is not None and len(v) == 0:
            raise ValueError("graders_match_only_if_reported_on must be null or non-empty (got empty list)")
        return v

    @field_validator("files", mode="before")
    @classmethod
    def normalize_files(cls, v: dict) -> dict[str, list[LineRange] | None]:
        """Convert flexible line specs to canonical list[LineRange] form.

        Input shapes:
          [10, 20] → [LineRange(10, 20)]
          [[10, 15], [20, 25]] → [LineRange(10, 15), LineRange(20, 25)]
          null → null

        Dict format with optional notes:
          {start_line: 42, end_line: 42, note: "why this matters"} → [LineRange(42, 42, "why this matters")]
          [{start_line: 10, end_line: 15, note: "..."}, ...] → [LineRange(10, 15, "..."), ...]

        Returns dict with normalized values (list[LineRange] or None).
        """
        normalized: dict[str, list[LineRange] | None] = {}
        for file_path, spec in v.items():
            if spec is None:
                # No specific lines: keep as None
                normalized[file_path] = None
            elif isinstance(spec, dict):
                # Dict format: {start_line: 42, end_line: 42, note: "..."}
                if "start_line" not in spec:
                    raise ValueError(f"Dict format for {file_path} requires 'start_line' field")
                if "end_line" not in spec:
                    raise ValueError(f"Dict format for {file_path} requires 'end_line' field")
                normalized[file_path] = [
                    LineRange(start_line=spec["start_line"], end_line=spec["end_line"], note=spec.get("note"))
                ]
            elif isinstance(spec, list):
                if not spec:
                    raise ValueError(f"Empty list not allowed for {file_path} (use null for no lines)")

                # Check first element to determine format
                first = spec[0]
                if isinstance(first, int):
                    # Range: [10, 20] → [LineRange(10, 20)]
                    if len(spec) != 2:
                        raise ValueError(f"Line range for {file_path} must have exactly 2 elements, got {len(spec)}")
                    normalized[file_path] = [LineRange(start_line=spec[0], end_line=spec[1])]
                elif isinstance(first, list):
                    # Multiple ranges: [[10, 15], [20, 25]] → [LineRange(10, 15), LineRange(20, 25)]
                    ranges = []
                    for r in spec:
                        if not isinstance(r, list) or len(r) != 2 or not all(isinstance(x, int) for x in r):
                            raise ValueError(f"Invalid range in {file_path}: {r} (must be [start, end])")
                        ranges.append(LineRange(start_line=r[0], end_line=r[1]))
                    normalized[file_path] = ranges
                elif isinstance(first, dict):
                    # List of dicts: [{start_line: 10, end_line: 15, note: "..."}, ...]
                    ranges = []
                    for r in spec:
                        if not isinstance(r, dict):
                            raise ValueError(f"Mixed types in {file_path} (expected all dicts)")
                        if "start_line" not in r or "end_line" not in r:
                            raise ValueError(f"Dict in {file_path} requires start_line and end_line: {r}")
                        ranges.append(LineRange(start_line=r["start_line"], end_line=r["end_line"], note=r.get("note")))
                    normalized[file_path] = ranges
                else:
                    raise ValueError(f"Invalid list element type in {file_path}: {type(first).__name__}")
            else:
                raise ValueError(
                    f"Invalid line spec for {file_path}: {spec} (type: {type(spec).__name__}). "
                    "Expected [start, end], [[r1_start, r1_end], ...], dict, or null"
                )
        return normalized

    def _build_files_dict(self) -> dict[Path, list[LineRange] | None]:
        """Convert normalized files to Path keys (values are already LineRange objects)."""
        return {Path(file_str): ranges_val for file_str, ranges_val in self.files.items()}

    def to_tp_occurrence(self) -> TruePositiveOccurrence:
        """Expand to canonical TruePositiveOccurrence."""
        if self.critic_scopes_expected_to_recall is None:
            raise ValueError(
                "critic_scopes_expected_to_recall required for TP occurrence (should be auto-inferred by validator)"
            )

        return TruePositiveOccurrence(
            occurrence_id=self.occurrence_id,
            files=self._build_files_dict(),
            note=self.note,
            critic_scopes_expected_to_recall={
                frozenset(Path(p) for p in trigger_set) for trigger_set in self.critic_scopes_expected_to_recall
            },
            graders_match_only_if_reported_on={Path(p) for p in self.graders_match_only_if_reported_on}
            if self.graders_match_only_if_reported_on
            else None,
        )

    def to_fp_occurrence(self) -> FalsePositiveOccurrence:
        """Expand to canonical FalsePositiveOccurrence."""
        if self.relevant_files is None:
            raise ValueError("relevant_files required for FP occurrence (should be auto-inferred by validator)")

        return FalsePositiveOccurrence(
            occurrence_id=self.occurrence_id,
            files=self._build_files_dict(),
            note=self.note,
            relevant_files={Path(p) for p in self.relevant_files},
            graders_match_only_if_reported_on={Path(p) for p in self.graders_match_only_if_reported_on}
            if self.graders_match_only_if_reported_on
            else None,
        )


class YAMLIssue(BaseModel):
    """Top-level YAML issue model - permissive input with validation.

    Enforces business rules:
    - Multi-occurrence issues must have notes on all occurrences
    - Single-file TPs can omit critic_scopes_expected_to_recall (auto-inferred)
    - Multi-file TPs must have explicit critic_scopes_expected_to_recall
    - FPs can omit relevant_files (auto-inferred from files keys)
    """

    rationale: str = Field(description="Full explanation of the issue")
    should_flag: bool = Field(description="True for TP, False for FP")
    occurrences: list[YAMLOccurrence] = Field(description="Issue occurrences")

    @model_validator(mode="after")
    def validate_multi_occurrence_notes(self) -> YAMLIssue:
        """Enforce note requirement for multi-occurrence issues."""
        if len(self.occurrences) > 1:
            for occ in self.occurrences:
                if occ.note is None:
                    raise ValueError(
                        f"Occurrence {occ.occurrence_id} missing required note "
                        "(multi-occurrence issues must have notes on all occurrences)"
                    )
        return self

    @model_validator(mode="after")
    def auto_infer_critic_scopes_expected_to_recall(self) -> YAMLIssue:
        """Auto-infer critic_scopes_expected_to_recall for single-file TPs."""
        if self.should_flag:
            for occ in self.occurrences:
                if occ.critic_scopes_expected_to_recall is None:
                    files = list(occ.files.keys())
                    if len(files) == 1:
                        # Auto-infer: single file → [[that_file]]
                        occ.critic_scopes_expected_to_recall = [[files[0]]]
                    else:
                        raise ValueError(
                            f"Multi-file TP occurrence {occ.occurrence_id} requires "
                            f"explicit critic_scopes_expected_to_recall (found files: {files})"
                        )
        return self

    @model_validator(mode="after")
    def validate_fp_relevant_files(self) -> YAMLIssue:
        """Ensure FPs have relevant_files set (can auto-infer from files)."""
        if not self.should_flag:
            for occ in self.occurrences:
                if occ.relevant_files is None:
                    # Auto-infer from files keys
                    occ.relevant_files = list(occ.files.keys())
        return self

    def to_true_positive(self, tp_id: str, snapshot_slug: SnapshotSlug) -> TruePositive:
        """Expand to canonical TruePositive model."""
        if not self.should_flag:
            raise ValueError("Cannot convert FP (should_flag=false) to TruePositive")

        return TruePositive(
            tp_id=tp_id,
            snapshot_slug=snapshot_slug,
            rationale=Rationale(self.rationale),
            occurrences=[occ.to_tp_occurrence() for occ in self.occurrences],
        )

    def to_false_positive(self, fp_id: str, snapshot_slug: SnapshotSlug) -> FalsePositive:
        """Expand to canonical FalsePositive model."""
        if self.should_flag:
            raise ValueError("Cannot convert TP (should_flag=true) to FalsePositive")

        return FalsePositive(
            fp_id=fp_id,
            snapshot_slug=snapshot_slug,
            rationale=Rationale(self.rationale),
            occurrences=[occ.to_fp_occurrence() for occ in self.occurrences],
        )

    model_config = ConfigDict(extra="forbid")
