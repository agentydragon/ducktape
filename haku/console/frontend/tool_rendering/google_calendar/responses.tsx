// Result rendering for the in-process `google_calendar` server (the argument-side widgets live
// in ./requests.tsx). The Zod schema is the FastMCP-advertised output schema for
// `create_calendar_event`, generated in mcp_tool_result_schema.ts from tools/list: the tool's
// CreateCalendarEventResult (haku/console/tools/google_calendar_client.py). Its Calendar-API
// `id`/`htmlLink` aliases are validation-only, so the serialized wire shape is exactly
// `{event_id, html_link}`; a missing field fails the parse (→ raw JSON fallback).

import { Anchor, Stack } from "@mantine/core";
import type { z } from "zod";

import { mcpToolResultSchema } from "../../mcp_tool_result_schema.ts";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry.tsx";
import { PreviewText } from "../vocabulary.tsx";
import { GOOGLE_CALENDAR_SERVER_ID } from "./requests.tsx";

const zCreateCalendarEventResult = mcpToolResultSchema(GOOGLE_CALENDAR_SERVER_ID, "create_calendar_event");

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
        <PreviewText size="xs" c="dimmed">
          event {result.event_id}
        </PreviewText>
      )}
    </Stack>
  );
}

/** Per-tool result widgets for the `google_calendar` server. */
export const googleCalendarResultPreviews = {
  create_calendar_event: defineResultPreview(zCreateCalendarEventResult, CreateCalendarEventResultView),
} satisfies Record<string, ToolResultPreview>;
