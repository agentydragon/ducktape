// The shared drawing of a shell command and what it printed, for the SSH `exec` Action and for a
// harness's own shell tool calls: the command as highlighted shell, and its output as text, each
// capped in height with the rest one click away.
import { Stack, Text } from "@mantine/core";
import type { JSX } from "react";

import { ClampedBlock, lineCount } from "./clamped_block";
import { CodeBlock } from "./code_block";
import { HighlightedText } from "./json_view";

export const COMMAND_MAX_HEIGHT_REM = 10;
export const OUTPUT_MAX_HEIGHT_REM = 16;

type Expansion = readonly [boolean, (expanded: boolean) => void];

/** The call as the reader judges it: what the model said it is for, where it runs, the exact
 * command, and the settings that change how it runs. */
export function CommandCallView({
  description,
  target,
  command,
  notes,
  expansion,
}: {
  description?: string;
  /** Where the command runs, such as `user@host`. */
  target?: string;
  command: string;
  notes: readonly string[];
  expansion?: Expansion;
}): JSX.Element {
  return (
    <Stack gap={4}>
      {description && <Text size="sm">{description}</Text>}
      {target && <CodeBlock text={target} presentation="label" />}
      <ClampedBlock maxHeightRem={COMMAND_MAX_HEIGHT_REM} lines={lineCount(command)} expansion={expansion}>
        <CodeBlock text={command} language="bash" />
      </ClampedBlock>
      {notes.map((note) => (
        <Text key={note} size="xs" c="dimmed">
          {note}
        </Text>
      ))}
    </Stack>
  );
}

/** What a command printed, as text, or as JSON when that is what it printed. `name` says which
 * stream it is, and `note` what is wrong with it. */
export function OutputBlock({
  name,
  note,
  text,
  expansion,
}: {
  name: string;
  note?: string;
  text: string;
  expansion?: Expansion;
}): JSX.Element {
  return (
    <div>
      <Text size="xs" c="dimmed" mb={4}>
        {name}
        {note && (
          <Text span size="xs" c="orange">
            {" "}
            · {note}
          </Text>
        )}
      </Text>
      <ClampedBlock maxHeightRem={OUTPUT_MAX_HEIGHT_REM} lines={lineCount(text)} expansion={expansion}>
        {/* A final newline ends the last line rather than starting an empty one. */}
        <HighlightedText text={text.replace(/\n$/, "")} />
      </ClampedBlock>
    </div>
  );
}
