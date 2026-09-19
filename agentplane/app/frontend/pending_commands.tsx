import { Box, Button, Group, Paper, ScrollArea, Stack, Text } from "@mantine/core";
import type { JSX } from "react";

import type { Command } from "../../protocol/command_pb";
import { commandLabel } from "./command_state";
import type { CommandSubmission } from "./command_submission";

function CommandText({ command }: { command: Command }): JSX.Element {
  return (
    <>
      <Text size="sm" fw={600}>
        {commandLabel(command)}
      </Text>
      {command.operation.case === "submitInput" && (
        <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {command.operation.value.text}
        </Text>
      )}
    </>
  );
}

export function PendingCommands({
  commands,
  retryDisabled,
  raw,
}: {
  commands: CommandSubmission;
  retryDisabled: boolean;
  raw: boolean;
}): JSX.Element | null {
  const pending = commands.observed.filter((value) => value.outcome === null);
  const nonEffects = commands.observed.filter(
    (value) =>
      value.outcome?.event?.observation.case === "commandFailed" ||
      value.outcome?.event?.observation.case === "commandNoop"
  );
  if (!pending.length && !commands.local.length && !nonEffects.length) return null;
  return (
    <Box>
      {pending.length + commands.local.length > 0 && (
        <Text size="xs" c="dimmed" mb={4}>
          Pending commands ({pending.length + commands.local.length})
        </Text>
      )}
      <ScrollArea.Autosize mah={220} type="always" offsetScrollbars>
        <Stack gap="xs" role="region" aria-label="Pending commands">
          {commands.local.map((value) => (
            <Paper key={value.command.commandId} data-command-id={value.command.commandId} withBorder p="xs">
              <CommandText command={value.command} />
              <Group justify="space-between">
                <Text size="xs" c="dimmed" role="status">
                  {value.admission ? "Saved · replay catching up" : "Awaiting saved confirmation"}
                  {commands.sending.has(value.command.commandId) && " · Sending…"}
                </Text>
                {!value.admission && (
                  <Button
                    size="compact-xs"
                    variant="subtle"
                    disabled={retryDisabled || commands.sending.has(value.command.commandId)}
                    onClick={() => void commands.retry(value)}
                  >
                    Retry
                  </Button>
                )}
              </Group>
              {!value.admission && commands.errors.has(value.command.commandId) && (
                <Text size="xs" c="orange">
                  {commands.errors.get(value.command.commandId)}
                </Text>
              )}
              {raw && (
                <Text size="xs" c="dimmed">
                  command {value.command.commandId} · locally submitted {new Date(value.submittedAt).toISOString()}
                </Text>
              )}
            </Paper>
          ))}
          {pending.map((value) => (
            <Paper key={value.command.commandId} data-command-id={value.command.commandId} withBorder p="xs">
              <CommandText command={value.command} />
              <Text size="xs" c="dimmed" role="status">
                Saved · awaiting effect
              </Text>
              {raw && (
                <Text size="xs" c="dimmed">
                  command {value.command.commandId} · admitted at event {String(value.admission.cursor)}
                </Text>
              )}
            </Paper>
          ))}
        </Stack>
        {nonEffects.length > 0 && (
          <Stack gap="xs" role="region" aria-label="Command outcomes" mt="xs">
            {nonEffects.map((value) => {
              const outcome = value.outcome?.event?.observation;
              if (outcome?.case !== "commandFailed" && outcome?.case !== "commandNoop") return null;
              return (
                <Paper key={value.command.commandId} data-command-id={value.command.commandId} withBorder p="xs">
                  <CommandText command={value.command} />
                  <Text size="xs" c="orange">
                    {outcome.case === "commandFailed" ? "Failed" : "No effect"}: {outcome.value.reason}
                  </Text>
                </Paper>
              );
            })}
          </Stack>
        )}
      </ScrollArea.Autosize>
    </Box>
  );
}
