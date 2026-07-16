// `google_calendar` preview screenshot entry — esbuild bundles this into the `:previews` IIFE.
// Holds the fixtures plus the mount call; the Calendar-only MCP stub is imported before the
// registry/widget graph reaches client.ts. `satisfies
// RegisteredToolPreviewFixture` ties each (serverId, toolName, args, result?) to the registry's
// real Zod schemas, so a stale id, argument, or result shape is a type error.
import "./preview_mock.ts";

import { mountPreviewCards } from "../screenshot/mount.tsx";

import type { RegisteredToolPreviewFixture } from "../index.tsx";

const PREVIEW_FIXTURES = [
  {
    title: "Create recurring training sessions",
    serverId: "google_calendar",
    toolName: "create_event",
    args: {
      summary: "Strength training",
      start: { date_time: "2026-07-21T18:00:00-07:00", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-07-21T19:00:00-07:00", time_zone: "America/Los_Angeles" },
      location: "Mission Bay gym",
      description: "Progressive overload session.",
      calendar_id: "family@group.calendar.google.com",
      reminders: [{ method: "popup", minutes_before_start: 30 }],
      attendees: ["training@example.com"],
      recurrence: ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;COUNT=12"],
    },
    result: {
      event_id: "0k5rq2n8vd1m3jf7",
      status: "confirmed",
      summary: "Strength training",
      start: { date_time: "2026-07-21T18:00:00-07:00", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-07-21T19:00:00-07:00", time_zone: "America/Los_Angeles" },
      recurrence: ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;COUNT=12"],
      html_link: "https://www.google.com/calendar/event?eid=MGs1cnEybjh2ZDFtM2pmNyBmYW1pbHlAZ3JvdXA",
    },
  },
  {
    title: "Get a recurring series",
    serverId: "google_calendar",
    toolName: "get_event",
    args: { event_id: "series-standup" },
    result: {
      event_id: "series-standup",
      status: "confirmed",
      summary: "Team standup",
      start: { date_time: "2026-07-20T09:30:00-07:00", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-07-20T09:45:00-07:00", time_zone: "America/Los_Angeles" },
      recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"],
      attendees: [
        { email: "alice@example.com", display_name: "Alice" },
        { email: "bob@example.com", display_name: "Bob" },
      ],
      html_link: "https://calendar.google.com/calendar/event?eid=series-standup",
    },
  },
  {
    title: "List calendar events",
    serverId: "google_calendar",
    toolName: "list_events",
    args: {
      time_min: "2026-07-20T00:00:00Z",
      time_max: "2026-07-27T00:00:00Z",
      query: "planning",
    },
    result: {
      events: [
        {
          event_id: "planning-series",
          summary: "Weekly planning",
          start: { date_time: "2026-07-20T10:00:00-07:00", time_zone: "America/Los_Angeles" },
          end: { date_time: "2026-07-20T11:00:00-07:00", time_zone: "America/Los_Angeles" },
          recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
        },
        {
          event_id: "quarterly-planning",
          summary: "Quarterly planning",
          start: { date: "2026-07-24" },
          end: { date: "2026-07-25" },
        },
      ],
      next_page_token: "next-page",
    },
  },
  {
    title: "List series instances",
    serverId: "google_calendar",
    toolName: "list_event_instances",
    args: { recurring_event_id: "series-standup", max_results: 25 },
    result: {
      events: [
        {
          event_id: "series-standup_20260720T163000Z",
          summary: "Team standup",
          start: { date_time: "2026-07-20T09:30:00-07:00", time_zone: "America/Los_Angeles" },
          end: { date_time: "2026-07-20T09:45:00-07:00", time_zone: "America/Los_Angeles" },
          recurring_event_id: "series-standup",
          original_start_time: {
            date_time: "2026-07-20T09:30:00-07:00",
            time_zone: "America/Los_Angeles",
          },
        },
      ],
    },
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
