"""google_calendar Action presentation examples."""

import json

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.actions.testing.action_presentation_cases import (
    CALENDAR_ID,
    CALENDAR_LOCAL_RANGE,
    CALENDAR_NAME,
    CALENDAR_RECORDED_NAME,
    ActionExample,
    ActionPresentationCases,
)
from agentplane.app.frontend.testing.app import AgentplaneFixture
from testing.frontend_visual.capture import VisualPage
from testing.frontend_visual.viewports import MOBILE_TOUCH, Viewport

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

CALENDAR_START = {"date_time": "2026-11-23T09:00:00-08:00", "time_zone": "America/Los_Angeles"}

CALENDAR_END = {"date_time": "2026-11-23T09:30:00-08:00", "time_zone": "America/Los_Angeles"}

CALENDAR_SERIES = {
    "event_id": "facilities-series",
    "summary": "Facilities coordination",
    "html_link": "https://calendar.google.com/calendar/event?eid=facilities-series",
    "status": "confirmed",
    "start": CALENDAR_START,
    "end": CALENDAR_END,
    "location": "Meeting room 2",
    "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
    "attendees": [
        {"email": "coordinator@example.test", "display_name": "Facilities coordinator", "response_status": "accepted"}
    ],
}

CALENDAR_ALL_DAY = {
    "event_id": "office-closure",
    "summary": "Office closure",
    "html_link": "https://calendar.google.com/calendar/event?eid=office-closure",
    "start": {"date": "2026-11-26"},
    "end": {"date": "2026-11-28"},
}

CALENDAR_OCCURRENCE = {
    "event_id": "facilities-series_20261130T170000Z",
    "status": "cancelled",
    "recurring_event_id": "facilities-series",
    "original_start_time": {"date_time": "2026-11-30T09:00:00-08:00", "time_zone": "America/Los_Angeles"},
}

CALENDAR_WINDOW = {"time_min": "2026-11-23T00:00:00-08:00", "time_max": "2026-12-01T00:00:00-08:00"}


def calendar_preview(event: dict[str, object] | None = None) -> tuple[str, dict[str, object], str]:
    payload: dict[str, object] = {"calendar": {"calendarId": CALENDAR_ID, "summary": CALENDAR_NAME}}
    if event is not None:
        payload["event"] = event
    return ("google-calendar-event-preview", payload, CALENDAR_NAME)


def calendar_event_preview(
    event_id: str, summary: str, scope: str, start: dict[str, str], end: dict[str, str]
) -> tuple[str, dict[str, object], str]:
    return calendar_preview(
        {
            "eventId": event_id,
            "summary": summary,
            "htmlLink": f"https://calendar.google.com/calendar/event?eid={event_id}",
            "start": start,
            "end": end,
            "scope": f"GOOGLE_CALENDAR_EVENT_SCOPE_{scope}",
        }
    )


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "google_calendar",
            "create_event",
            {
                "summary": CALENDAR_SERIES["summary"],
                "calendar_id": CALENDAR_ID,
                "start": CALENDAR_START,
                "end": CALENDAR_END,
                "location": "Meeting room 2",
                "attendees": ["coordinator@example.test"],
                "reminders": [{"method": "email", "minutes_before_start": 15}],
                "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
            },
            CALENDAR_SERIES,
            "Facilities coordination",
            live_preview=calendar_preview(),
        ),
        ActionExample(
            "google_calendar",
            "update_event",
            {
                "event_id": "facilities-series",
                "calendar_id": CALENDAR_ID,
                "summary": "Facilities coordination (rescheduled)",
                "location": "",
                "attendees": [],
                "reminders": [],
            },
            {**CALENDAR_SERIES, "summary": "Facilities coordination (rescheduled)", "location": None, "attendees": []},
            "Facilities coordination (rescheduled)",
            live_preview=calendar_event_preview(
                "facilities-series",
                "Facilities coordination",
                "SERIES",
                {"dateTime": "2026-11-23T09:00:00-08:00", "timeZone": "America/Los_Angeles"},
                {"dateTime": "2026-11-23T09:30:00-08:00", "timeZone": "America/Los_Angeles"},
            ),
        ),
        ActionExample(
            "google_calendar",
            "delete_event",
            {"event_id": "facilities-series_20261130T170000Z", "calendar_id": CALENDAR_ID},
            None,
            "Deletion confirmed.",
            live_preview=calendar_event_preview(
                "facilities-series_20261130T170000Z",
                "Facilities coordination",
                "OCCURRENCE",
                {"dateTime": "2026-11-30T09:00:00-08:00", "timeZone": "America/Los_Angeles"},
                {"dateTime": "2026-11-30T09:30:00-08:00", "timeZone": "America/Los_Angeles"},
            ),
        ),
        ActionExample(
            "google_calendar",
            "get_event",
            {"event_id": "office-closure", "calendar_id": CALENDAR_ID},
            CALENDAR_ALL_DAY,
            "All day",
            live_preview=calendar_event_preview(
                "office-closure", "Office closure", "SINGLE_EVENT", {"date": "2026-11-26"}, {"date": "2026-11-28"}
            ),
        ),
        ActionExample(
            "google_calendar",
            "list_events",
            {"calendar_id": CALENDAR_ID, **CALENDAR_WINDOW, "query": "facilities"},
            {
                "events": [CALENDAR_SERIES, CALENDAR_ALL_DAY],
                "summary": CALENDAR_RECORDED_NAME,
                "next_page_token": "next-page",
            },
            "More events are available.",
            live_preview=calendar_preview(),
        ),
        ActionExample(
            "google_calendar",
            "list_event_instances",
            {"calendar_id": CALENDAR_ID, "recurring_event_id": "facilities-series", **CALENDAR_WINDOW},
            {
                "events": [
                    {
                        **CALENDAR_SERIES,
                        "event_id": "facilities-series_20261123T170000Z",
                        "recurrence": [],
                        "html_link": "https://calendar.google.com/calendar/event?eid=facilities-series_20261123T170000Z",
                        "recurring_event_id": "facilities-series",
                    },
                    CALENDAR_OCCURRENCE,
                ]
            },
            "Cancelled",
            live_preview=calendar_event_preview(
                "facilities-series",
                "Facilities coordination",
                "SERIES",
                {"dateTime": "2026-11-23T09:00:00-08:00", "timeZone": "America/Los_Angeles"},
                {"dateTime": "2026-11-23T09:30:00-08:00", "timeZone": "America/Los_Angeles"},
            ),
        ),
    )


@pytest.mark.parametrize("viewport", [MOBILE_TOUCH], ids=["touch"])
async def test_calendar_source_time_touch(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    args: dict[str, object] = {
        "summary": "Facilities coordination",
        "start": CALENDAR_START,
        "end": CALENDAR_END,
        "calendar_id": CALENDAR_ID,
    }
    await app.show_action_preview(
        "google_calendar", "create_event", args, {"google-calendar-event-preview": calendar_preview()[1]}
    )
    await app.show_action_receipt(
        "google_calendar",
        "create_event",
        args,
        {"content": [{"type": "text", "text": json.dumps(CALENDAR_SERIES)}], "structuredContent": CALENDAR_SERIES},
    )
    await app.mount_action_presentations(sidebar_width=viewport.width - 16, detail_width=viewport.width - 32, mcp=True)
    sidebar = view.page.locator("[data-action-visual=sidebar]")
    when = sidebar.get_by_role("button", name=CALENDAR_LOCAL_RANGE, exact=True)
    await when.tap()
    tooltip = view.page.get_by_role("tooltip")
    await expect(tooltip).to_contain_text("2026-11-23T09:00:00-08:00 · America/Los_Angeles")
    await expect(sidebar.get_by_role("button", name="Expand ", exact=False)).to_have_attribute("aria-expanded", "false")
    await view.capture()


if __name__ == "__main__":
    pytest_bazel.main()
