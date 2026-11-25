"""Framework-independent Twenty Questions bookkeeping and result files."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from skills.info_gathering.evals.twenty_questions.result_types import LogEntry, Player, Result, Timeout


@dataclass
class GameRecord:
    turn_limit: int
    turn: int = 0
    result: Result | None = None
    invalid_input_count: int = 0
    log_entries: list[LogEntry] = field(default_factory=list)

    def record(self, player: Player, content: str, tool_name: str, /, **tool_args: object) -> None:
        self.log_entries.append(
            LogEntry(
                timestamp=datetime.now(UTC),
                player=player,
                content=content,
                tool_calls=[{"name": tool_name, "args": tool_args}],
            )
        )

    def check_turn_limit(self) -> None:
        if self.turn >= self.turn_limit and self.result is None:
            self.result = Timeout(limit=self.turn_limit)

    def finish(self) -> Result:
        if self.result is None:
            self.result = Timeout(limit=self.turn_limit)
        return self.result


def write_transcript(calls_path: Path, log_entries: Iterable[LogEntry]) -> None:
    with calls_path.open("w") as f:
        for entry in log_entries:
            f.write(entry.model_dump_json() + "\n")
