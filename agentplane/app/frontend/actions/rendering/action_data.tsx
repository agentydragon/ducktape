import { Code, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import type { ActionObject, ActionPresentationSpec } from "../presentation_catalog";
import { definePreview, type ArgumentsPreview, type PreviewProps } from "./entry";
import { defineResultPreview, type ResultPreview, type ResultPreviewProps } from "./result_entry";

const objectSchema = z.record(z.string(), z.unknown());
const resultSchema = z.unknown();

const FIELD_LABELS: Readonly<Record<string, string>> = {
  apiVersion: "API version",
  best_before_date: "Best-before date",
  calendar_id: "Calendar",
  container: "Container",
  event_id: "Event",
  fieldSelector: "Field selector",
  field_id: "Field",
  gracePeriodSeconds: "Grace period (seconds)",
  labelSelector: "Label selector",
  label_ids: "Labels",
  location_id: "Location",
  max_results: "Maximum results",
  message_id: "Message",
  namespace: "Namespace",
  node_id: "Node",
  product_id: "Product",
  query: "Search query",
  repo: "Repository",
  thread_id: "Thread",
  thread_ids: "Threads",
  timeout_seconds: "Timeout (seconds)",
  time_max: "End time",
  time_min: "Start time",
  to: "Recipients",
};

function fieldLabel(key: string): string {
  const known = FIELD_LABELS[key];
  if (known !== undefined) return known;
  const spaced = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replaceAll("_", " ")
    .replace(/\s+/g, " ")
    .trim();
  return spaced.length === 0 ? key : spaced[0]!.toUpperCase() + spaced.slice(1);
}

function shortValue(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") {
    const text = value.replace(/\s+/g, " ").trim();
    return text.length > 100 ? `${text.slice(0, 97)}…` : text;
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "empty list";
    if (value.every((item) => ["string", "number", "boolean"].includes(typeof item))) {
      const shown = value
        .slice(0, 3)
        .map((item) => String(item))
        .join(", ");
      return value.length > 3 ? `${shown}, … (${value.length} total)` : shown;
    }
    return `${value.length} ${value.length === 1 ? "item" : "items"}`;
  }
  if (typeof value === "object") {
    const count = Object.keys(value).length;
    return `${count} ${count === 1 ? "field" : "fields"}`;
  }
  return String(value);
}

function StructuredValue({ value, depth = 0 }: { value: unknown; depth?: number }): JSX.Element {
  if (value === null)
    return (
      <Text size="sm" c="dimmed">
        null
      </Text>
    );
  if (typeof value === "string") {
    return (
      <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        {value}
      </Text>
    );
  }
  if (typeof value === "number" || typeof value === "boolean") return <Code>{String(value)}</Code>;
  if (Array.isArray(value)) {
    if (value.length === 0)
      return (
        <Text size="sm" c="dimmed">
          Empty list
        </Text>
      );
    return (
      <Stack gap={4}>
        {value.map((item, index) => (
          <div key={index}>
            {Array.isArray(item) || (typeof item === "object" && item !== null) ? (
              <Text size="xs" c="dimmed" mb={2}>
                Item {index + 1}
              </Text>
            ) : null}
            <StructuredValue value={item} depth={depth + 1} />
          </div>
        ))}
      </Stack>
    );
  }
  if (typeof value === "object") {
    if (depth >= 5) return <Code>{JSON.stringify(value)}</Code>;
    return <StructuredFields value={value as Record<string, unknown>} depth={depth + 1} />;
  }
  return <Code>{String(value)}</Code>;
}

function StructuredFields({
  value,
  exclude = [],
  include,
  depth = 0,
}: {
  value: Record<string, unknown>;
  exclude?: readonly string[];
  include?: readonly string[];
  depth?: number;
}): JSX.Element {
  const selected = include === undefined ? Object.keys(value) : include.filter((key) => key in value);
  const keys = selected.filter((key) => !exclude.includes(key));
  if (keys.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No additional arguments.
      </Text>
    );
  }
  return (
    <Stack gap="xs">
      {keys.map((key) => (
        <div key={key}>
          <Text size="xs" c="dimmed" fw={600} mb={2}>
            {fieldLabel(key)}
          </Text>
          <StructuredValue value={value[key]} depth={depth} />
        </div>
      ))}
    </Stack>
  );
}

function ActionLabel({ args, spec }: PreviewProps<ActionObject> & { spec: ActionPresentationSpec }): JSX.Element {
  return <Text fw={600}>{spec.label(args)}</Text>;
}

function ActionSummary({ args, spec }: PreviewProps<ActionObject> & { spec: ActionPresentationSpec }): JSX.Element {
  const summary = (spec.summaryFields ?? [])
    .filter((key) => key in args)
    .map((key) => `${fieldLabel(key)}: ${shortValue(args[key])}`)
    .join(" · ");
  return (
    <Text size="xs" style={{ overflowWrap: "anywhere" }}>
      {summary || "No additional summary fields"}
    </Text>
  );
}

function OpenedArguments({ args, spec }: PreviewProps<ActionObject> & { spec: ActionPresentationSpec }): JSX.Element {
  return <StructuredFields value={args} exclude={spec.labelFields} />;
}

function FullArguments({ args }: PreviewProps<ActionObject>): JSX.Element {
  return <StructuredFields value={args} />;
}

function ActionResult({ result, title }: ResultPreviewProps<unknown> & { title: string }): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed" fw={600}>
        {title}
      </Text>
      <StructuredValue value={result} />
    </Stack>
  );
}

export interface ActionDataPresentation {
  label: ArgumentsPreview;
  pane: {
    collapsed?: ArgumentsPreview;
    opened: ArgumentsPreview;
    requestTitleIsRedundant: (title: string, args: unknown) => boolean;
  };
  details: {
    arguments: ArgumentsPreview;
    result?: ResultPreview;
  };
}

/** Build the common readable view slots for a catalog entry; action wording remains per-action. */
export function actionDataPresentation(spec: ActionPresentationSpec): ActionDataPresentation {
  const label = definePreview(objectSchema, (props) => <ActionLabel {...props} spec={spec} />);
  const collapsed =
    spec.summaryFields !== undefined && spec.summaryFields.length > 0
      ? definePreview(objectSchema, (props) => <ActionSummary {...props} spec={spec} />)
      : undefined;
  const opened = definePreview(objectSchema, (props) => <OpenedArguments {...props} spec={spec} />);
  const argumentsPreview = definePreview(objectSchema, FullArguments);
  const result =
    spec.resultLabel === undefined
      ? undefined
      : defineResultPreview(resultSchema, (props) => <ActionResult {...props} title={spec.resultLabel!} />);

  const requestTitleIsRedundant = (title: string, args: unknown): boolean => {
    if (typeof args !== "object" || args === null || Array.isArray(args)) return false;
    const labelText = spec.label(args as ActionObject);
    const normalize = (value: string): string =>
      value
        .toLowerCase()
        .replace(/[^\p{L}\p{N}]+/gu, " ")
        .trim();
    return normalize(title) === normalize(labelText);
  };

  return {
    label,
    pane: { ...(collapsed === undefined ? {} : { collapsed }), opened, requestTitleIsRedundant },
    details: { arguments: argumentsPreview, ...(result === undefined ? {} : { result }) },
  };
}
