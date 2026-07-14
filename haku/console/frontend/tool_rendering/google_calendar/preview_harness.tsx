// `google_calendar` preview screenshot entry — esbuild bundles this into the `:previews` IIFE.
// Holds the fixtures plus the mount call; `mount` is imported FIRST so its fetch stub (mock.ts)
// is installed before the registry/widget graph reaches client.ts. `satisfies
// RegisteredToolPreviewFixture` ties each (serverId, toolName, args, result?) to the registry's
// real Zod schemas, so a stale id, argument, or result shape is a type error.
import { mountPreviewCards } from "../screenshot/mount.tsx";

import type { RegisteredToolPreviewFixture } from "../index.tsx";

const PREVIEW_FIXTURES = [
  {
    title: "Schedule dentist appointment",
    serverId: "google_calendar",
    toolName: "create_calendar_event",
    args: {
      summary: "Dentist appointment",
      start: { date_time: "2026-07-12T09:00:00", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-07-12T10:00:00", time_zone: "America/Los_Angeles" },
      location: "123 Market St, San Francisco",
      description: "Routine cleaning and checkup.",
      calendar_id: "family@group.calendar.google.com",
      reminders: [{ method: "popup", minutes_before_start: 30 }],
      attendees: ["dentist@example.com"],
    },
    result: {
      event_id: "0k5rq2n8vd1m3jf7",
      html_link: "https://www.google.com/calendar/event?eid=MGs1cnEybjh2ZDFtM2pmNyBmYW1pbHlAZ3JvdXA",
    },
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
