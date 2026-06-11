"""Result types for the function learning eval."""

from typing import Literal

from pydantic import BaseModel, Field

from skills.info_gathering.evals.harness import RunSummary as _BaseRunSummary


class ProgramError(BaseModel):
    line: int = Field(description="0-based line index (= input value)")
    error: str


class ProgramScore(BaseModel):
    """Full scoring breakdown for one program submission."""

    hamming_loss: int = Field(description="Total bit disagreements across all inputs")
    parse_errors: int = Field(default=0, description="Lines that didn't parse as an integer")
    out_of_range: int = Field(default=0, description="Lines that parsed but were outside [0, max_output]")
    missing_lines: int = Field(default=0, description="Lines missing (program produced too few lines)")
    examples: list[ProgramError] = Field(default_factory=list, description="Up to 5 example errors")

    @property
    def has_errors(self) -> bool:
        return self.parse_errors > 0 or self.out_of_range > 0 or self.missing_lines > 0


class TurnResult(BaseModel):
    turn: int
    query: int
    query_result: int
    score: ProgramScore


class FunctionLearningResult(BaseModel):
    kind: Literal["completed"] = "completed"
    total_hamming_loss: int
    per_turn_losses: list[int]
    solved_at_turn: int | None = Field(
        default=None, description="Turn on which 0 loss was first achieved; None if never solved"
    )


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class RunSummary(_BaseRunSummary[FunctionLearningResult]):
    function_name: str
    n_bits: int
    m_bits: int
    usage: TokenUsage = Field(default_factory=TokenUsage)
