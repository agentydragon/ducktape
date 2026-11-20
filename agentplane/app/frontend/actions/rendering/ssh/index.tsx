import { Badge, Group, Stack, Text } from "@mantine/core";
import { z } from "zod";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { defineResultPreview } from "../result_entry";
import { execPresentation } from "./exec";

const listArguments = z.strictObject({});
const listTargets = defineResultPreview(
  z.array(
    z.object({
      host: z.string(),
      user: z.string(),
      available: z.boolean(),
      error: z.object({ message: z.string() }).nullish(),
    })
  ),
  ({ result }) => (
    <Stack gap="xs">
      {result.length === 0 && <Text size="sm">No SSH targets configured.</Text>}
      {result.map((target) => (
        <Stack key={`${target.user}@${target.host}`} gap={2}>
          <Group gap="xs">
            <Text size="sm" fw={600}>
              {target.user}@{target.host}
            </Text>
            <Badge variant="light" color={target.available ? "green" : "gray"}>
              {target.available ? "Available" : "Unavailable"}
            </Badge>
          </Group>
          {target.error && <Text size="sm">{target.error.message}</Text>}
        </Stack>
      ))}
    </Stack>
  )
);

export const sshRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["exec", execPresentation],
  [
    "list_targets",
    {
      fallbackLabel: "List SSH targets",
      details: { arguments: { schema: listArguments, render: () => null }, result: listTargets },
      compactCardExpands: expandsForRejectedArguments(listArguments),
    },
  ],
]);
