import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { Badge, Code, Group, Stack, Table, Text } from "@mantine/core";
import { z } from "zod";

import { ShellCommandView, ShellCommandSummary, OutputBlock } from "../../shell_command_view";
import { sandboxExecNotes } from "../schemas/sandbox/exec";
import { definePreview, type ArgumentsPreview } from "./entry";
import { defineResultPreview, type ResultPreview } from "./result_entry";

const named = z.strictObject({ name: z.string().min(1) });
const create = named.extend({ template: z.string() });
const exec = named.extend({
  script: z.string().min(1),
  timeout_seconds: z.number().int().positive(),
  max_output_bytes: z.number().int().nonnegative(),
  cwd: z.string().nullable().optional(),
});
const empty = z.strictObject({});

export const sandboxExecLabel: ArgumentsPreview = definePreview(exec, ({ args }) => (
  <Text size="sm" fw={600}>
    Run command in sandbox <Code>{args.name}</Code>
  </Text>
));
export const sandboxExecCollapsed: ArgumentsPreview = definePreview(exec, ({ args }) => (
  <ShellCommandSummary text={args.script} />
));
export const sandboxExecArguments: ArgumentsPreview = definePreview(exec, ({ args }) => (
  <ShellCommandView command={args.script} notes={sandboxExecNotes(args)} releaseWhenFar surface="framed" />
));
export const sandboxCreateLabel: ArgumentsPreview = definePreview(create, ({ args }) => (
  <Text size="sm" fw={600}>
    Create sandbox <Code>{args.name}</Code>
  </Text>
));
export const sandboxCreateArguments: ArgumentsPreview = definePreview(create, ({ args }) => (
  <Text size="sm">
    Template: <Code>{args.template}</Code>
  </Text>
));
export const sandboxGetLabel: ArgumentsPreview = definePreview(named, ({ args }) => (
  <Text size="sm" fw={600}>
    Inspect sandbox <Code>{args.name}</Code>
  </Text>
));
export const sandboxDisposeLabel: ArgumentsPreview = definePreview(named, ({ args }) => (
  <Text size="sm" fw={600}>
    Dispose sandbox <Code>{args.name}</Code>
  </Text>
));
// The label carries every argument; direct previews preserve null as the slot suppression signal.
export const sandboxNamedArguments: ArgumentsPreview = { schema: named, render: () => null };
export const sandboxListLabel: ArgumentsPreview = definePreview(empty, () => (
  <Text size="sm" fw={600}>
    List sandboxes
  </Text>
));
export const sandboxListArguments: ArgumentsPreview = { schema: empty, render: () => null };

// The label is the whole call.
function argumentsMatch(schema: z.ZodType): (args: unknown) => boolean {
  return (args) => schema.safeParse(args).success;
}

const condition = z.strictObject({
  type: z.string(),
  status: z.string(),
  reason: z.string().nullable().optional(),
  message: z.string().nullable().optional(),
  last_transition_time: z.string().nullable().optional(),
});
const info = z.strictObject({
  name: z.string(),
  template: z.string(),
  phase: z.string().nullable().optional(),
  conditions: z.array(condition),
  created_at: z.string().nullable().optional(),
  node_name: z.string().nullable().optional(),
  pod_ips: z.array(z.string()).optional(),
});

function SandboxInfo({ value }: { value: z.infer<typeof info> }) {
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Code>{value.name}</Code>
        <Badge>{value.phase ?? "Phase unknown"}</Badge>
      </Group>
      <Text size="sm">
        Template: <Code>{value.template}</Code>
      </Text>
      {value.node_name && (
        <Text size="sm">
          Node: <Code>{value.node_name}</Code>
        </Text>
      )}
      {value.created_at && <Text size="sm">Created: {value.created_at}</Text>}
      {value.pod_ips?.length ? <Text size="sm">IP addresses: {value.pod_ips.join(", ")}</Text> : null}
      {value.conditions.length === 0 ? (
        <Text size="sm" c="dimmed">
          No conditions reported.
        </Text>
      ) : (
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Condition</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Details</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {value.conditions.map((item, i) => (
              <Table.Tr key={i}>
                <Table.Td>{item.type}</Table.Td>
                <Table.Td>{item.status}</Table.Td>
                <Table.Td>
                  {item.reason && <Text size="sm">{item.reason}</Text>}
                  {item.message && <Text size="sm">{item.message}</Text>}
                  {item.last_transition_time && (
                    <Text size="xs" c="dimmed">
                      {item.last_transition_time}
                    </Text>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
export const sandboxInfoResult: ResultPreview = defineResultPreview(info, ({ result }) => (
  <SandboxInfo value={result} />
));
export const sandboxListResult: ResultPreview = defineResultPreview(
  z.strictObject({ sandboxes: z.array(info) }),
  ({ result }) => (
    <Stack gap="md">
      {result.sandboxes.length === 0 ? (
        <Text size="sm" c="dimmed">
          No sandboxes.
        </Text>
      ) : (
        result.sandboxes.map((value) => <SandboxInfo key={value.name} value={value} />)
      )}
    </Stack>
  )
);
export const sandboxDisposeResult: ResultPreview = defineResultPreview(
  z.strictObject({ name: z.string(), existed: z.boolean() }),
  ({ result }) => (
    <Text size="sm">
      Sandbox <Code>{result.name}</Code>: {result.existed ? "disposed" : "already absent"}.
    </Text>
  )
);
const stream = z.union([
  z.string(),
  z.strictObject({ truncated_text: z.string(), total_bytes: z.number().int().nonnegative() }),
]);
const execution = z.strictObject({
  exit: z.discriminatedUnion("kind", [
    z.strictObject({ kind: z.literal("exited"), exit_code: z.number().int() }),
    z.strictObject({ kind: z.literal("killed"), signal: z.number().int() }),
    z.strictObject({ kind: z.literal("timed_out") }),
  ]),
  stdout: stream,
  stderr: stream,
  duration_seconds: z.number().nonnegative(),
});
export const sandboxExecResult: ResultPreview = defineResultPreview(execution, ({ result }) => (
  <Stack gap="xs">
    <Group gap="xs">
      <Badge color={result.exit.kind === "exited" && result.exit.exit_code === 0 ? "green" : "red"}>
        {result.exit.kind === "exited"
          ? `Exit ${result.exit.exit_code}`
          : result.exit.kind === "killed"
            ? `Killed by signal ${result.exit.signal}`
            : "Timed out"}
      </Badge>
      <Text size="sm">{result.duration_seconds} s</Text>
    </Group>
    {(["stdout", "stderr"] as const).map((name) => {
      const value = result[name];
      return value === "" ? null : (
        <OutputBlock
          key={name}
          name={name}
          text={typeof value === "string" ? value : value.truncated_text}
          note={typeof value === "string" ? undefined : `truncated by the server; ${value.total_bytes} total bytes`}
          releaseWhenFar
          surface="framed"
        />
      );
    })}
    {result.stdout === "" && result.stderr === "" && (
      <Text size="sm" c="dimmed">
        No output.
      </Text>
    )}
  </Stack>
));

export const sandboxExecPresentation: ActionPresentation = {
  label: sandboxExecLabel,
  pane: { collapsed: sandboxExecCollapsed, opened: sandboxExecArguments },
  details: { arguments: sandboxExecArguments, nativeResult: sandboxExecResult },
};

export const sandboxCreatePresentation: ActionPresentation = {
  label: sandboxCreateLabel,
  pane: { collapsed: sandboxCreateArguments, opened: sandboxCreateArguments },
  details: { arguments: sandboxCreateArguments, nativeResult: sandboxInfoResult },
  offersCardDecisionButtons: argumentsMatch(create),
  compactCardExpands: expandsForRejectedArguments(create),
};

export const sandboxGetPresentation: ActionPresentation = {
  label: sandboxGetLabel,
  details: { arguments: sandboxNamedArguments, nativeResult: sandboxInfoResult },
  offersCardDecisionButtons: argumentsMatch(named),
  compactCardExpands: expandsForRejectedArguments(named),
};

export const sandboxDisposePresentation: ActionPresentation = {
  label: sandboxDisposeLabel,
  details: { arguments: sandboxNamedArguments, nativeResult: sandboxDisposeResult },
  offersCardDecisionButtons: argumentsMatch(named),
  compactCardExpands: expandsForRejectedArguments(named),
};

export const sandboxListPresentation: ActionPresentation = {
  label: sandboxListLabel,
  details: { arguments: sandboxListArguments, nativeResult: sandboxListResult },
  offersCardDecisionButtons: argumentsMatch(empty),
  compactCardExpands: expandsForRejectedArguments(empty),
};

export const sandboxRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["exec", sandboxExecPresentation],
  ["create", sandboxCreatePresentation],
  ["get", sandboxGetPresentation],
  ["dispose", sandboxDisposePresentation],
  ["list", sandboxListPresentation],
]);
