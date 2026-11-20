import { Image, Stack, Text } from "@mantine/core";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { defineResultPreview } from "../result_entry";
import { haAction } from "./secondary_common";

const cameraResult = defineResultPreview(
  z.object({
    content: z
      .array(
        z.object({
          type: z.literal("image"),
          data: z.string(),
          mimeType: z.string().regex(/^image\/[\w.+-]+$/),
        })
      )
      .length(1),
    structuredContent: z.never().optional(),
  }),
  ({
    result: {
      content: [image],
    },
  }) => (
    <Stack gap={4}>
      <Text size="sm" fw={600}>
        Camera snapshot
      </Text>
      <Image src={`data:${image.mimeType};base64,${image.data}`} alt="Home Assistant camera snapshot" fit="contain" />
    </Stack>
  )
);

export const homeAssistantHistoryRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "ha_get_history",
    haAction({
      title: "Read Home Assistant history",
      target: ["entity_ids"],
      fields: [
        "end_time",
        "entity_ids",
        "fields",
        "limit",
        "minimal_response",
        "offset",
        "order",
        "period",
        "significant_changes_only",
        "source",
        "start_time",
        "statistic_types",
      ],
      required: ["entity_ids"],
      result: { fields: ["source", "period_type", "warnings"], rows: ["entities"] },
    }),
  ],
  [
    "ha_get_logs",
    haAction({
      title: "Read Home Assistant logs",
      target: ["entity_id", "slug"],
      fields: [
        "compact",
        "end_time",
        "entity_id",
        "hours_back",
        "level",
        "limit",
        "offset",
        "order",
        "search",
        "slug",
        "source",
        "structured",
        "top_n",
      ],
      result: {
        fields: ["source", "warnings", "returned_entries", "returned_lines"],
        rows: ["entries", "loggers", "issues"],
        text: ["log"],
      },
    }),
  ],
  [
    "ha_get_automation_traces",
    haAction({
      title: "Read automation traces",
      target: ["automation_id"],
      fields: ["automation_id", "deduplicate", "detailed", "limit", "offset", "order", "run_id", "sections"],
      required: ["automation_id"],
      result: {
        fields: ["state", "script_execution", "trigger", "trace_count", "diagnostics"],
        rows: ["traces", "actions", "conditions"],
        objects: ["config_summary", "diagnostics"],
      },
    }),
  ],
  [
    "ha_get_camera_image",
    haAction({
      title: "Capture Home Assistant camera image",
      target: ["entity_id"],
      fields: ["entity_id", "height", "width"],
      required: ["entity_id"],
      envelopeResult: cameraResult,
    }),
  ],
  [
    "ha_get_system_health",
    haAction({
      title: "Read Home Assistant health",
      target: ["config_entry_id", "device_id"],
      fields: [
        "config_entry_id",
        "device_id",
        "diagnostics_data_limit",
        "diagnostics_data_offset",
        "diagnostics_data_path",
        "diagnostics_fields",
        "diagnostics_truncate_at_bytes",
        "include",
        "include_dismissed_repairs",
      ],
      result: {
        fields: ["component_count", "baseline_available", "warnings"],
        rows: ["health_info", "repairs"],
        objects: ["config_check", "diagnostics", "zha_network", "zwave_network", "thread_network", "matter_network"],
      },
    }),
  ],
  [
    "ha_get_operation_status",
    haAction({
      title: "Check Home Assistant operation status",
      target: ["operation_id"],
      fields: ["operation_id", "timeout_seconds"],
      required: ["operation_id"],
      result: {
        fields: [
          "status",
          "entity_id",
          "action",
          "final_state",
          "expected_state",
          "time_remaining_ms",
          "total_operations",
          "completed",
          "pending",
          "failed",
          "not_found",
        ],
        rows: ["detailed_results"],
        reportsPhase: true,
      },
    }),
  ],
  [
    "ha_get_entity_exposure",
    haAction({
      title: "Read voice assistant exposure",
      target: ["entity_id"],
      fields: ["assistant", "entity_id"],
      result: {
        fields: ["is_exposed_anywhere", "has_custom_settings", "note"],
        rows: ["exposed_to", "exposed_entities", "summary"],
      },
    }),
  ],
  [
    "ha_get_integration",
    haAction({
      title: "Read Home Assistant integration",
      target: ["domain", "entry_id"],
      fields: [
        "device_id",
        "diagnostics_data_limit",
        "diagnostics_data_offset",
        "diagnostics_data_path",
        "diagnostics_fields",
        "diagnostics_truncate_at_bytes",
        "domain",
        "entry_id",
        "exact_match",
        "include_diagnostics",
        "include_knx_project",
        "include_options",
        "include_schema",
        "include_subentries",
        "include_subentry_schema",
        "limit",
        "offset",
        "query",
        "show_advanced_options",
        "subentry_id",
        "subentry_type",
      ],
      result: {
        fields: ["state", "title", "domain", "status", "warnings"],
        rows: ["entries", "subentries"],
        objects: ["entry", "config_entry", "diagnostics"],
      },
    }),
  ],
  [
    "ha_get_todo",
    haAction({
      title: "Read Home Assistant to-do lists",
      target: ["entity_id"],
      fields: ["entity_id", "status"],
      result: { fields: ["status_filter"], rows: ["items", "todo_lists"] },
      modeTitle: (args) => (args.entity_id ? "Read to-do items" : "List Home Assistant to-do lists"),
    }),
  ],
  [
    "ha_set_todo_item",
    haAction({
      title: "Change Home Assistant to-do item",
      target: ["entity_id", "item"],
      fields: ["description", "due_date", "due_datetime", "entity_id", "item", "rename", "status", "summary"],
      required: ["entity_id"],
      result: { fields: ["item", "description", "due_date", "due_datetime"], objects: ["updates"] },
      modeTitle: (args) => (args.item ? "Update to-do item" : "Add to-do item"),
    }),
  ],
  [
    "ha_remove_todo_item",
    haAction({
      title: "Remove Home Assistant to-do item",
      target: ["entity_id", "item"],
      fields: ["entity_id", "item"],
      required: ["entity_id", "item"],
      result: { fields: ["item"], outcome: "Item removed" },
    }),
  ],
  [
    "ha_config_get_calendar_events",
    haAction({
      title: "Read Home Assistant calendar",
      target: ["entity_id"],
      fields: ["end", "entity_id", "max_results", "start"],
      required: ["entity_id"],
      result: { fields: ["count", "total_available"], rows: ["events"] },
    }),
  ],
  [
    "ha_config_set_calendar_event",
    haAction({
      title: "Create Home Assistant calendar event",
      target: ["entity_id", "summary"],
      fields: ["description", "end", "entity_id", "location", "rrule", "start", "summary"],
      required: ["entity_id", "summary", "start", "end"],
      result: { objects: ["event"], outcome: "Event created" },
      modeTitle: (args) => (args.rrule ? "Create recurring calendar event" : "Create calendar event"),
    }),
  ],
  [
    "ha_config_remove_calendar_event",
    haAction({
      title: "Remove Home Assistant calendar event",
      target: ["entity_id", "uid"],
      fields: ["entity_id", "recurrence_id", "recurrence_range", "uid"],
      required: ["entity_id", "uid"],
      result: { fields: ["recurrence_id", "recurrence_range"], outcome: "Deletion confirmed" },
      modeTitle: (args) =>
        args.recurrence_range === "THIS_AND_FUTURE"
          ? "Delete this and future occurrences"
          : args.recurrence_id
            ? "Delete one occurrence"
            : "Delete entire event or series",
    }),
  ],
]);
