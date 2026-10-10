import { Code, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { JsonView } from "../../json_view";
import type { ActionObject, ActionPresentationSpec } from "../presentation_catalog";
import { definePreview, type ArgumentsPreview, type PreviewProps } from "./entry";
import { defineResultPreview, type ResultPreview, type ResultPreviewProps } from "./result_entry";

const objectSchema = z.record(z.string(), z.unknown());
const resultSchema = z.unknown();

function fieldLabel(key: string): string {
  const spaced = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replaceAll("_", " ")
    .replace(/\s+/g, " ")
    .trim();
  return spaced.length === 0 ? key : spaced[0]!.toUpperCase() + spaced.slice(1);
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

function StructuredFields({ value, depth = 0 }: { value: Record<string, unknown>; depth?: number }): JSX.Element {
  const keys = Object.keys(value);
  if (keys.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No arguments.
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

function FullArguments({ args }: PreviewProps<ActionObject>): JSX.Element {
  return <JsonView value={args} />;
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

export interface ActionPresentationFallback {
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

/** Generic fallback only. Actions with a purpose-built widget register their own DOM in each slot. */
export function fallbackActionPresentation(spec: ActionPresentationSpec): ActionPresentationFallback {
  const label = definePreview(objectSchema, (props) => <ActionLabel {...props} spec={spec} />);
  const fullArguments = definePreview(objectSchema, FullArguments);
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
    pane: { opened: fullArguments, requestTitleIsRedundant },
    details: { arguments: fullArguments, ...(result === undefined ? {} : { result }) },
  };
}
