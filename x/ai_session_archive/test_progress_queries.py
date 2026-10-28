# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
"""Read-committed progress polling while an importer registers a new machine."""

from datetime import UTC, datetime

import pytest
import pytest_bazel
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import ORMExecuteState, sessionmaker

from x.ai_session_archive.database import MachineRow, ResticSnapshotRow, SnapshotFileRow
from x.ai_session_archive.queries import list_import_progress

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


@pytest.mark.parametrize("register_before_query", [2, 3])
def test_new_machine_committed_during_progress_poll_is_seen_next_time(
    postgres_engine: Engine, register_before_query: int
) -> None:
    factory = sessionmaker(postgres_engine)
    with factory.begin() as writer:
        writer.add(MachineRow(id="known", display_name="Known machine"))

    with factory() as reader:
        query_count = 0

        def register_machine(_state: ORMExecuteState) -> None:
            nonlocal query_count
            query_count += 1
            if query_count != register_before_query:
                return
            with factory.begin() as writer:
                writer.add(MachineRow(id="new", display_name="New machine"))
                writer.add(
                    ResticSnapshotRow(
                        machine_id="new",
                        snapshot_id="snapshot",
                        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
                        paths=[],
                        inventory_status="pending",
                        processing_status="pending",
                        codex_metadata_status="pending",
                    )
                )
                writer.add(
                    SnapshotFileRow(
                        machine_id="new",
                        snapshot_id="snapshot",
                        path="/transcript.jsonl",
                        change_kind="added",
                        file_type="file",
                        status="pending",
                    )
                )

        event.listen(reader, "do_orm_execute", register_machine)
        first_poll = list_import_progress(reader)
        event.remove(reader, "do_orm_execute", register_machine)
        assert query_count == 3
        assert [machine.machine_id for machine in first_poll] == ["known"]

        second_poll = list_import_progress(reader)
        assert [machine.machine_id for machine in second_poll] == ["known", "new"]
        assert second_poll[1].known_snapshot_count == 1
        assert second_poll[1].pending_file_count == 1


if __name__ == "__main__":
    pytest_bazel.main()
