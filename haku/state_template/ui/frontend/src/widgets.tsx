import { Alert, Anchor, Badge, Table, Text } from "@mantine/core";
import type { ReactNode } from "react";

import type { PropagationEntry, PropagationTarget } from "./types.ts";

// The fixed registry of "standard widgets" that garden content (run notes, memory notes,
// procedures) may embed with literal-attribute syntax: <Callout>, <StatusBadge>. Rendered by
// the non-eval markdown pipeline (see mdx.tsx) — no arbitrary props/expressions, only string
// attributes, since there's no JS evaluation to compute anything richer.
// PropagationMatrix takes a structured `data` prop that only a real caller can supply, so it's
// used directly as TSX (by the Runs detail page below), not embedded in authored markdown.

export type CalloutKind = "info" | "warning" | "success" | "danger";
const CALLOUT_COLOR: Record<CalloutKind, string> = {
  info: "blue",
  warning: "yellow",
  success: "teal",
  danger: "red",
};

export function Callout({
  kind = "info",
  title,
  children,
}: {
  kind?: CalloutKind;
  title?: string;
  children?: ReactNode;
}) {
  return (
    <Alert color={CALLOUT_COLOR[kind]} title={title} my="sm" variant="light">
      {children}
    </Alert>
  );
}

export function StatusBadge({ status, color = "gray" }: { status: string; color?: string }) {
  return (
    <Badge variant="light" color={color}>
      {status}
    </Badge>
  );
}

// created/updated = it landed; no_change = considered, didn't apply; n/a = never applies. Plain
// colored text, not a badge — one dot-badge per row (often 20+ rows/run) was the "everything's a
// chip" clutter the operator called out (2026-07-02); a color-coded word carries the same signal.
const ACTION_COLOR: Record<PropagationTarget["action"], string> = {
  created: "teal",
  updated: "teal",
  no_change: "dimmed",
  "n/a": "dimmed",
};

// A surface path that the garden can render — `.md`/`.mdx` (the garden browses the curated dirs
// GARDEN_DIRS = memory/, procedures/, runs/ in garden.tsx). Non-garden surfaces (items/*.yaml,
// kitchen/board.yaml, ui/) render as plain path text — there's no page to deep-link them into yet.
function isGardenFile(path: string): boolean {
  return /\.mdx?$/.test(path);
}

// Renders a run's propagation[] (or any change→surfaces list) as a matrix: one row per surface,
// the change spanning its surfaces' rows. `onNavigate`, when given, turns garden-eligible surface
// paths into in-app links (the same affordance the Mdx renderer gives internal markdown links) —
// omit it (e.g. when this widget is embedded in authored markdown, with no router in scope) to
// fall back to plain path text.
export function PropagationMatrix({
  data,
  onNavigate,
}: {
  data: PropagationEntry[];
  onNavigate?: (path: string) => void;
}) {
  if (data.length === 0) return null;
  return (
    <Table withTableBorder withColumnBorders my="sm" fz="sm" verticalSpacing="xs">
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Change</Table.Th>
          <Table.Th>Surface</Table.Th>
          <Table.Th>Action</Table.Th>
          <Table.Th>Note</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {data.flatMap((p) =>
          p.surfaces.map((s, j) => (
            <Table.Tr key={`${p.change}-${j}`}>
              {j === 0 && (
                <Table.Td rowSpan={p.surfaces.length || 1}>
                  {p.change}
                  {p.source && (
                    <Text span c="dimmed" size="xs">
                      {" "}
                      ({p.source})
                    </Text>
                  )}
                </Table.Td>
              )}
              <Table.Td>
                {onNavigate && isGardenFile(s.surface) ? (
                  <Anchor size="sm" onClick={() => onNavigate(s.surface)} style={{ cursor: "pointer" }}>
                    {s.surface}
                  </Anchor>
                ) : (
                  <Text size="sm" ff="monospace">
                    {s.surface}
                  </Text>
                )}
              </Table.Td>
              <Table.Td>
                <Text size="sm" c={ACTION_COLOR[s.action]} fw={600}>
                  {s.action}
                </Text>
              </Table.Td>
              <Table.Td>{s.note}</Table.Td>
            </Table.Tr>
          ))
        )}
      </Table.Tbody>
    </Table>
  );
}

// Passed as the `components` scope to the non-eval markdown renderer (see mdx.tsx). No
// PropagationMatrix here — see the comment above it for why.
export const WIDGETS = { Callout, StatusBadge };
