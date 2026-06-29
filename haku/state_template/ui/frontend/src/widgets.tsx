import { Alert, Badge, Table, Text } from "@mantine/core";
import type { ReactNode } from "react";

import type { PropagationEntry, PropagationTarget } from "./types.ts";

// The fixed registry of "standard widgets" that garden content (run notes, memory notes,
// procedures) may use from MDX: <Callout>, <StatusBadge>, <PropagationMatrix data={…}/>.
// The registry IS the trust surface — authored MDX can only reach these components, never
// arbitrary app internals. Add a widget here to make it available everywhere the garden renders.
// PropagationMatrix is also used directly (as TSX) by the Runs detail page, so the structured
// run data and an MDX-embedded matrix render identically.

type CalloutKind = "info" | "warning" | "success" | "danger";
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

// updated = it landed; no_change = considered, didn't apply; n/a = surface never applies.
const ACTION_COLOR: Record<PropagationTarget["action"], string> = {
  updated: "teal",
  no_change: "gray",
  "n/a": "gray",
};

// Renders a run's propagation[] (or any change→surfaces list) as a matrix: one row per surface,
// the change spanning its surfaces' rows. The richer presentation of the same data the Runs tab
// used to show as inline badges.
export function PropagationMatrix({ data }: { data: PropagationEntry[] }) {
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
              <Table.Td>{s.surface}</Table.Td>
              <Table.Td>
                <Badge size="sm" variant="dot" color={ACTION_COLOR[s.action]}>
                  {s.action}
                </Badge>
              </Table.Td>
              <Table.Td>{s.note}</Table.Td>
            </Table.Tr>
          ))
        )}
      </Table.Tbody>
    </Table>
  );
}

// Passed as the `components` scope to runtime-evaluated MDX (see mdx.tsx).
export const WIDGETS = { Callout, StatusBadge, PropagationMatrix };
