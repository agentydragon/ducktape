// The shared drawing of a shell command and what it printed, for the SSH `exec` Action and for a
// harness's own shell tool calls: the command as highlighted shell, and its output as text, each
// capped in height with the rest one click away.
import { Button, Stack, Text } from "@mantine/core";
import { type JSX, useState } from "react";

import { ClampedBlock, lineCount } from "./clamped_block";
import { CodeBlock } from "./code_block";
import { Disclosure, DisclosureBody } from "./disclosure";
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
    <Stack gap={0}>
      {(description || target) && (
        <DisclosureBody>
          <Stack gap={4}>
            {description && <Text size="sm">{description}</Text>}
            {target && <CodeBlock text={target} presentation="label" />}
          </Stack>
        </DisclosureBody>
      )}
      <ClampedBlock
        maxHeightRem={COMMAND_MAX_HEIGHT_REM}
        lines={lineCount(command)}
        label="Command"
        expansion={expansion}
      >
        <CodeBlock text={command} language="bash" surface="plain" />
      </ClampedBlock>
      {notes.length > 0 && (
        <DisclosureBody>
          <Stack gap={4}>
            {notes.map((note) => (
              <Text key={note} size="xs" c="dimmed">
                {note}
              </Text>
            ))}
          </Stack>
        </DisclosureBody>
      )}
    </Stack>
  );
}

/** What a command printed, as text, or as JSON when that is what it printed. `name` says which
 * stream it is, and `note` what is wrong with it. */
export function OutputBlock({
  name,
  note,
  text,
  raw = false,
  expansion,
  disclosure,
}: {
  name: string;
  note?: string;
  text: string;
  /** Preserve the recorded output text, including its final newline. */
  raw?: boolean;
  expansion?: Expansion;
  /** Retain the nested output disclosure while a tool row leaves the DOM. */
  disclosure?: Expansion;
}): JSX.Element {
  const localDisclosure = useState(true);
  const localExpansion = useState(false);
  const previewExpansion = expansion ?? localExpansion;
  const [expanded, setExpanded] = previewExpansion;
  const [open, setOpen] = disclosure ?? localDisclosure;
  const lines = lineCount(text);

  return (
    <Disclosure
      className="agentplane-output-disclosure"
      summary={
        <Text className="agentplane-output-label" size="sm" c="dimmed">
          {name}
          <Text span size="sm" c="dimmed">
            {` · ${lines} ${lines === 1 ? "line" : "lines"}`}
          </Text>
          {note && (
            <Text span size="xs" c="orange">
              {" "}
              · {note}
            </Text>
          )}
        </Text>
      }
      summaryAside={
        expanded && (
          <Button
            size="compact-xs"
            variant="subtle"
            color="gray"
            aria-label={`Show less ${name}`}
            onClick={() => setExpanded(false)}
          >
            Show less
          </Button>
        )
      }
      open={open}
      onOpenChange={setOpen}
      keepMounted
    >
      <ClampedBlock
        maxHeightRem={OUTPUT_MAX_HEIGHT_REM}
        lines={lines}
        label={name}
        expansion={previewExpansion}
        stickyCollapse={false}
      >
        {/* Pretty output ends its last line with a final newline; Raw keeps that recorded newline. */}
        <HighlightedText text={raw ? text : text.replace(/\n$/, "")} surface="plain" />
      </ClampedBlock>
    </Disclosure>
  );
}
