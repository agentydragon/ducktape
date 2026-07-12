// Result rendering for the in-process `google_calendar` server (the argument-side widgets live
// in ./requests.tsx). `create_calendar_event` returns
// CreateCalendarEventResult {event_id, html_link} (haku/console/tools/google_calendar_client.py);
// depending on serialization aliasing the wire keys may be those Python field names or the
// Calendar API's own `id`/`htmlLink`, so the schema accepts either spelling and passes unknown
// keys through. Falls back to the raw-JSON view when no event link came back.

import { Anchor, Stack, Text } from "@mantine/core";
import { z } from "zod";

import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry.tsx";

const zCreateCalendarEventResult = z
  .looseObject({
    event_id: z.string().optional(),
    id: z.string().optional(),
    html_link: z.string().optional(),
    htmlLink: z.string().optional(),
  })
  // Normalize the two spellings; the pipe fails the parse (→ raw JSON fallback) when neither
  // link key is present.
  .transform((r) => ({ eventId: r.event_id ?? r.id ?? null, htmlLink: r.html_link ?? r.htmlLink }))
  .pipe(z.object({ eventId: z.string().nullable(), htmlLink: z.string() }));

type CreateCalendarEventResult = z.infer<typeof zCreateCalendarEventResult>;

function CreateCalendarEventResultView({ result, variant }: ResultPreviewProps<CreateCalendarEventResult>) {
  // The link is the outcome the operator acts on, so it is the whole compact form; detailed
  // adds the created event's id, dimmed, for correlating with the Calendar API.
  return (
    <Stack gap={2}>
      <Anchor href={result.htmlLink} target="_blank" rel="noreferrer" size="sm">
        Open event in Google Calendar ↗
      </Anchor>
      {variant === "detailed" && result.eventId && (
        <Text size="xs" c="dimmed">
          event {result.eventId}
        </Text>
      )}
    </Stack>
  );
}

/** Per-tool result widgets for the `google_calendar` server. */
export const googleCalendarResultPreviews = {
  create_calendar_event: defineResultPreview(zCreateCalendarEventResult, CreateCalendarEventResultView),
} satisfies Record<string, ToolResultPreview>;
