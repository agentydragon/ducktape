from agentplane.history_service.api.history import history_service_pb2
from agentplane.proto import event_pb2

# gazelle:include_dep @pypi//protobuf


def events_page(after_cursor: int, *events: event_pb2.Event) -> history_service_pb2.ReadSessionEventsResponse:
    page = history_service_pb2.ReadSessionEventsResponse(last_cursor=after_cursor + len(events))
    for cursor, event in enumerate(events, start=after_cursor + 1):
        page.entries.add(cursor=cursor, event=event)
    return page
