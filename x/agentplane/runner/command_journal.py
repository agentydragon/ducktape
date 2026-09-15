"""Durable runner-side command support state.

The Agentplane app owns desired Thread commands.  This file is deliberately narrower: it gives one
runner session a crash-safe idempotency fence and records the native correlation needed to continue
the same command after a process restart.  The replayable Event log remains the public record.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from google.protobuf.json_format import MessageToDict, ParseDict

from x.agentplane.protocol import command_pb2
from x.agentplane.runner.event_log import Observation, decode_observation, encode_observation
from x.agentplane.runner.journal_file import JournalFile
from x.agentplane.runner.journal_lines import journal_lines


@dataclass(frozen=True)
class JournalEntry:
    command: command_pb2.Command
    state: str
    native_correlation: dict[str, str]
    outcome: Observation | None


class CommandConflictError(ValueError):
    """One command id was reused with different requested work."""


class CommandJournal:
    """Append-only admission/dispatch/effect/terminal records, synced before their public Event."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, JournalEntry] = {}
        self._order: list[str] = []
        if path.exists():
            self._load()
        self._file = JournalFile(path)

    @property
    def entries(self) -> Sequence[JournalEntry]:
        return [self._entries[command_id] for command_id in self._order]

    def get(self, command_id: str) -> JournalEntry | None:
        return self._entries.get(command_id)

    def admit(self, command: command_pb2.Command) -> bool:
        """Persist a command once. Returns false for an exact retry, rejects an id collision."""
        previous = self._entries.get(command.command_id)
        if previous is not None:
            if previous.command != command:
                raise CommandConflictError(f"command id {command.command_id!r} was reused for different work")
            return False
        self._append({"record": "admitted", "command": MessageToDict(command)})
        self._entries[command.command_id] = JournalEntry(command, "admitted", {}, None)
        self._order.append(command.command_id)
        return True

    def dispatch_planned(self, command_id: str, *, native_correlation: dict[str, str] | None = None) -> None:
        self._transition(command_id, "dispatch_planned", native_correlation=native_correlation)

    def native_effect_observed(self, command_id: str, *, native_correlation: dict[str, str] | None = None) -> None:
        self._transition(command_id, "native_effect_observed", native_correlation=native_correlation)

    def terminal(
        self, command_id: str, *, outcome: Observation, native_correlation: dict[str, str] | None = None
    ) -> None:
        """Persist the exact terminal public observation before its Event append."""
        self._transition(command_id, "terminal", native_correlation=native_correlation, outcome=outcome)

    def _transition(
        self,
        command_id: str,
        state: str,
        *,
        native_correlation: dict[str, str] | None = None,
        outcome: Observation | None = None,
    ) -> None:
        entry = self._entries.get(command_id)
        if entry is None:
            raise ValueError(f"unknown command id {command_id!r}")
        if entry.state == "terminal":
            return
        if state == "terminal" and outcome is None:
            raise ValueError("a terminal command journal record requires its causal observation")
        correlation = {**entry.native_correlation, **(native_correlation or {})}
        recorded_outcome = outcome if outcome is not None else entry.outcome
        row: dict[str, object] = {"record": state, "command_id": command_id, "native_correlation": correlation}
        if recorded_outcome is not None:
            row["outcome"] = encode_observation(recorded_outcome)
        self._append(row)
        self._entries[command_id] = JournalEntry(entry.command, state, correlation, recorded_outcome)

    def _load(self) -> None:
        for line_number, raw in enumerate(journal_lines(self.path), start=1):
            try:
                row = json.loads(raw)
                record = row["record"]
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"corrupt command journal {self.path} line {line_number}") from error
            if record == "admitted":
                command = ParseDict(row["command"], command_pb2.Command())
                if not command.command_id or command.WhichOneof("operation") is None:
                    raise ValueError(f"corrupt command journal {self.path} line {line_number}: invalid command")
                if command.command_id in self._entries:
                    raise ValueError(f"corrupt command journal {self.path} line {line_number}: duplicate command")
                self._entries[command.command_id] = JournalEntry(command, "admitted", {}, None)
                self._order.append(command.command_id)
                continue
            command_id = row.get("command_id")
            entry = self._entries.get(command_id)
            if entry is None or record not in {"dispatch_planned", "native_effect_observed", "terminal"}:
                raise ValueError(f"corrupt command journal {self.path} line {line_number}: invalid transition")
            correlation = row.get("native_correlation", entry.native_correlation)
            if not isinstance(correlation, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in correlation.items()
            ):
                raise ValueError(f"corrupt command journal {self.path} line {line_number}: invalid correlation")
            outcome = entry.outcome
            if record == "terminal":
                try:
                    outcome = decode_observation(row["outcome"])
                except (KeyError, ValueError) as error:
                    raise ValueError(
                        f"corrupt command journal {self.path} line {line_number}: invalid terminal outcome"
                    ) from error
            self._entries[command_id] = JournalEntry(entry.command, record, correlation, outcome)

    def _append(self, row: dict[str, object]) -> None:
        self._file.append((json.dumps(row, sort_keys=True) + "\n").encode())

    def close(self) -> None:
        self._file.close()
