"""Runner journal entries, as a runner's Attach replays them."""

from agentplane.proto import command_pb2, event_log_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf


def entry(cursor: int, event: event_pb2.Event) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id="test-journal", sequence=cursor), event=event
    )


def admission(command: command_pb2.Command, cursor: int = 1) -> event_log_pb2.EventEntry:
    return entry(cursor, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))


def journal_entry(cursor: int) -> event_log_pb2.EventEntry:
    return entry(cursor, event_pb2.Event(native=event_pb2.Native(line=f"line {cursor}")))
