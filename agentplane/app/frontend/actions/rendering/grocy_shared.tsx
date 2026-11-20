import { Anchor, Code, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import { definePreview, type PreviewProps } from "./entry";
import type { ActionOutcome, ActionPresentation } from "./presentation";
import type { ResultPreview } from "./result_entry";

export const GROCY_BASE: string = "https://grocy-sf.allegedly.works";
export const reference: z.ZodType<string | number> = z.union([z.number().int(), z.string()]);

export function Line({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
      {children}
    </Text>
  );
}

export function objectLink(path: string, id: number, name: string): JSX.Element {
  return (
    <Anchor href={`${GROCY_BASE}/${path}/${id}`} target="_blank" rel="noreferrer" inherit>
      {name}
    </Anchor>
  );
}

export function displayRef(value: string | number): ReactNode {
  return typeof value === "number" ? <Code>{value}</Code> : value;
}

export const batchError: z.ZodType<{ kind: "error"; error: string }> = z.strictObject({
  kind: z.literal("error"),
  error: z.string(),
});
export function batchOutcome(rows: { kind: "ok" | "error" }[]): ActionOutcome | undefined {
  const failures = rows.filter((row) => row.kind === "error").length;
  return failures === 0
    ? undefined
    : { label: failures === rows.length ? "All items failed" : "Partially completed", failed: true };
}

export function grocyRequest<S extends z.ZodType>(
  label: string,
  schema: S,
  preview: (props: PreviewProps<z.infer<S>>) => ReactNode,
  result: ResultPreview
): ActionPresentation {
  const argumentsPreview = definePreview(schema, preview);
  return { fallbackLabel: label, pane: { opened: argumentsPreview }, details: { arguments: argumentsPreview, result } };
}
