import { Code, Paper, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { TextBlock } from "../../../ui/text_block";
import { Disclosure, DisclosureBody } from "../../../../../../util/frontend/disclosure";
import { type KubernetesResource, isKubernetesResource } from "../../schemas/kubernetes_admin/manifest";
import { defineResultPreview, type ResultPreview } from "../result_entry";
import { KubernetesResourceSummary, kubernetesResources } from "./resources";

export function parseKubernetesTable(text: string): Record<string, string>[] | undefined {
  if (text.trim() === "") return [];
  const [heading, ...lines] = text.trim().split(/\r?\n/);
  const columns = heading.trim().split(/\t+| {2,}/);
  if (columns.length < 2 || !columns.includes("NAME") || new Set(columns).size !== columns.length) return undefined;
  const rows = lines.map((line) => line.trim().split(/\t+| {2,}/));
  if (rows.some((cells) => cells.length !== columns.length)) return undefined;
  return rows.map((cells) => Object.fromEntries(columns.map((key, i) => [key, cells[i]])));
}

function listObjects(value: unknown): KubernetesResource[] | undefined {
  const parsed = z.object({ kind: z.string().endsWith("List"), items: z.array(z.unknown()) }).safeParse(value);
  if (!parsed.success || !parsed.data.items.every(isKubernetesResource)) return undefined;
  return parsed.data.items;
}

const tableRow = z.object({
  NAME: z.string().min(1),
  NAMESPACE: z.string().optional(),
  STATUS: z.string().optional(),
  READY: z.string().optional(),
  RESTARTS: z.union([z.string(), z.number()]).optional(),
});

type ListRow = { resource: KubernetesResource } | { table: z.output<typeof tableRow> };
type ResourceList = { rows: ListRow[]; text?: string };

const envelope = z.object({
  content: z.array(z.object({ type: z.literal("text"), text: z.string() })).length(1),
  structuredContent: z.unknown().optional(),
});

function fromValues(items: unknown[]): ListRow[] | undefined {
  if (items.every(isKubernetesResource)) return items.map((resource) => ({ resource }));
  const rows = z.array(z.record(z.string(), z.unknown())).safeParse(items);
  if (!rows.success) return undefined;
  const normalised = rows.data.map((row) =>
    Object.fromEntries(Object.entries(row).map(([key, value]) => [key.toUpperCase(), value]))
  );
  const result = z.array(tableRow).safeParse(normalised);
  return result.success ? result.data.map((table) => ({ table })) : undefined;
}

const resourceList: z.ZodType<ResourceList> = envelope.transform((value, context) => {
  const text = value.content[0].text;
  const { structuredContent } = value;
  let rows: ListRow[] | undefined;
  if (structuredContent !== undefined) {
    const list = z.object({ items: z.array(z.unknown()) }).safeParse(structuredContent);
    if (list.success) rows = fromValues(list.data.items);
    if (rows === undefined && Array.isArray(structuredContent)) rows = fromValues(structuredContent);
  } else {
    const table = parseKubernetesTable(text);
    if (table) rows = fromValues(table);
    if (rows === undefined) {
      const resources = kubernetesResources(text);
      if (resources?.length === 1) {
        const items = listObjects(resources[0]);
        if (items) rows = items.map((resource) => ({ resource }));
      }
      rows ??= resources?.map((resource) => ({ resource }));
    }
    if (rows === undefined && text.trim() === "[]") rows = [];
  }
  if (rows === undefined) {
    context.addIssue({ code: "custom", message: "Expected a Kubernetes resource list" });
    return z.NEVER;
  }
  return { rows, text };
});

function ResourceRows({ result }: { result: ResourceList }): JSX.Element {
  return (
    <Stack gap="xs">
      {result.rows.length === 0 && (
        <Text size="sm" c="dimmed">
          No resources found.
        </Text>
      )}
      {result.rows.map((row, index) => {
        if ("resource" in row) return <KubernetesResourceSummary key={index} resource={row.resource} />;
        const { NAME, NAMESPACE, STATUS, READY, RESTARTS } = row.table;
        const observations = [
          STATUS,
          READY && `${READY} ready`,
          RESTARTS !== undefined && `${RESTARTS} restarts`,
        ].filter(Boolean);
        return (
          <Paper key={index} withBorder p="xs">
            <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
              <Code>{NAMESPACE ? `${NAMESPACE}/${NAME}` : NAME}</Code>
            </Text>
            {observations.length > 0 && <Text size="sm">{observations.join(" · ")}</Text>}
          </Paper>
        );
      })}
      {result.text?.trim() && (
        <Disclosure summary={<Text size="sm">Provider output</Text>}>
          <DisclosureBody>
            <TextBlock text={result.text} />
          </DisclosureBody>
        </Disclosure>
      )}
    </Stack>
  );
}

export const resourceListResult: ResultPreview = defineResultPreview(resourceList, ResourceRows);
