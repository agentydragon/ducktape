// Result rendering for the in-process `google_calendar` server (the argument-side widgets live
// in ./requests.tsx). `create_calendar_event` returns CreateCalendarEventResult
// (haku/console/tools/google_calendar_client.py), whose Calendar-API `id`/`htmlLink` aliases
// are validation-only — every dump emits the Python field names — so the wire shape is exactly
// `{event_id, html_link}`. Unknown extra keys pass through; a missing field fails the parse
// (→ raw JSON fallback).

import { Anchor, Stack, Text } from "@mantine/core";
import { z } from "zod";

import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry.tsx";

const zCreateCalendarEventResult = z.looseObject({
  event_id: z.string(),
  html_link: z.string(),
});

type CreateCalendarEventResult = z.infer<typeof zCreateCalendarEventResult>;

function CreateCalendarEventResultView({ result, variant }: ResultPreviewProps<CreateCalendarEventResult>) {
  // The link is the outcome the operator acts on, so it is the whole compact form; detailed
  // adds the created event's id, dimmed, for correlating with the Calendar API.
  return (
    <Stack gap={2}>
      <Anchor href={result.html_link} target="_blank" rel="noreferrer" size="sm">
        Open event in Google Calendar ↗
      </Anchor>
      {variant === "detailed" && (
        <Text size="xs" c="dimmed">
          event {result.event_id}
        </Text>
      )}
    </Stack>
  );
}

/** Per-tool result widgets for the `google_calendar` server. */
export const googleCalendarResultPreviews = {
  create_calendar_event: defineResultPreview(zCreateCalendarEventResult, CreateCalendarEventResultView),
} satisfies Record<string, ToolResultPreview>;
