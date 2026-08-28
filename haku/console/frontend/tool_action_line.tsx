import { Text } from "@mantine/core";

import { toolActionDescription } from "./tool_rendering/actions";

/** The card's identity line. A registered tool renders its own action description ("Gmail: Draft
 * email", "kubectl: Delete Pod" — destructive ones in red); a tool with no widget falls back to
 * `serverId.toolName`. One line, one style either way, so the header reads consistently and the
 * widget body needn't restate the action. */
export function ToolActionLine({
  serverId,
  toolName,
  args,
}: {
  serverId: string;
  toolName: string;
  args: Record<string, unknown> | null | undefined;
}): JSX.Element {
  const action = args ? toolActionDescription(serverId, toolName, args) : null;
  return (
    <Text size="xs" c={action?.destructive ? "red" : "dimmed"}>
      {action ? action.text : `${serverId}.${toolName}`}
    </Text>
  );
}
