// The shared drawing of a shell command and what it printed, for the SSH `exec` Action and for a
// harness's own shell tool calls: the command as highlighted shell, and its output as text, each
// capped in height with the rest one click away.
import { Button, Stack, Text } from "@mantine/core";
import { type JSX, useState } from "react";

import { argvText } from "./argv_text";
import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "./clamped_block";
import { CodeBlock, type CodeBlockSurface, InlineCode } from "../../../util/frontend/code_block";
import { Disclosure, DisclosureBody } from "./disclosure";
import { HighlightedText } from "./json_view";
import { OperationSection } from "./operation_section";

export const SHELL_COMMAND_MAX_HEIGHT_REM = 10;

/** How many lines of a command a compact card's collapsed row shows before cutting it off. */
export const SHELL_COMMAND_SUMMARY_MAX_LINES = 3;

type Expansion = readonly [boolean, (expanded: boolean) => void];

type ShellCommandInput = { command: string; argv?: never } | { command?: never; argv: readonly string[] };

/** A command on a compact card's collapsed row: highlighted shell, its line breaks read as spaces, cut
 * off after `SHELL_COMMAND_SUMMARY_MAX_LINES` lines. The opened preview and the full card hold all of it. */
export function ShellCommandSummary({ text }: { text: string }): JSX.Element {
  return (
    <Text size="xs" lineClamp={SHELL_COMMAND_SUMMARY_MAX_LINES} style={{ overflowWrap: "anywhere" }}>
      <InlineCode text={text} language="bash" />
    </Text>
  );
}

/** The call as the reader judges it: what the model said it is for, the exact command, and the
 * settings that change how it runs. Where it runs belongs in the Action's label. */
export function ShellCommandView(
  props: {
    description?: string;
    notes: readonly string[];
    expansion?: Expansion;
    /** Release CodeMirror's editor when this Action call is far outside the viewport. */
    releaseWhenFar?: boolean;
    /** Use the shared operation frame in Action hosts. Thread tool cards retain their own frame. */
    surface?: CodeBlockSurface;
    /** Keep the Command heading pinned as the collapse control while scrolling expanded content. */
    stickyCollapse?: boolean;
  } & ShellCommandInput
): JSX.Element {
  const { description, notes, expansion, releaseWhenFar = false, surface, stickyCollapse = true } = props;
  const command = "command" in props ? props.command : undefined;
  const argv = "argv" in props ? props.argv : undefined;
  const commandText = command ?? (argv === undefined ? "" : argvText(argv));
  const commandLabel = command === undefined ? "Arguments" : "Command";
  const content = (
    <Stack className="agentplane-command-call" gap={0}>
      {description && (
        <DisclosureBody>
          <Text size="sm">{description}</Text>
        </DisclosureBody>
      )}
      <ClampedBlock
        maxHeightRem={SHELL_COMMAND_MAX_HEIGHT_REM}
        lines={lineCount(commandText)}
        label={commandLabel}
        header={
          stickyCollapse ? undefined : (
            <Text className="agentplane-command-heading" size="sm" c="dimmed">
              {commandLabel}
            </Text>
          )
        }
        expansion={expansion}
        stickyCollapse={stickyCollapse}
      >
        <CodeBlock text={commandText} language="bash" surface="plain" releaseWhenFar={releaseWhenFar} />
      </ClampedBlock>
      {notes.length > 0 && (
        <DisclosureBody>
          <Text size="xs" c="dimmed">
            {notes.join(" · ")}
          </Text>
        </DisclosureBody>
      )}
    </Stack>
  );
  return surface === undefined ? content : <OperationSection surface={surface}>{content}</OperationSection>;
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
  releaseWhenFar = false,
  surface,
}: {
  name: string;
  note?: string;
  text: string;
  /** Preserve the recorded output text, including its final newline. */
  raw?: boolean;
  expansion?: Expansion;
  /** Retain the nested output disclosure while a tool row leaves the DOM. */
  disclosure?: Expansion;
  /** Release CodeMirror's editor when this Action result is far outside the viewport. */
  releaseWhenFar?: boolean;
  /** Use the shared operation frame in Action hosts. Thread tool cards retain their own frame. */
  surface?: CodeBlockSurface;
}): JSX.Element {
  const localDisclosure = useState(true);
  const localExpansion = useState(false);
  const previewExpansion = expansion ?? localExpansion;
  const [expanded, setExpanded] = previewExpansion;
  const [open, setOpen] = disclosure ?? localDisclosure;
  const lines = lineCount(text);

  const content = (
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
        maxHeightRem={TEXT_MAX_HEIGHT_REM}
        lines={lines}
        label={name}
        expansion={previewExpansion}
        stickyCollapse={false}
      >
        {/* Pretty output ends its last line with a final newline; Raw keeps that recorded newline. */}
        <HighlightedText text={raw ? text : text.replace(/\n$/, "")} surface="plain" releaseWhenFar={releaseWhenFar} />
      </ClampedBlock>
    </Disclosure>
  );
  return surface === undefined ? content : <OperationSection surface={surface}>{content}</OperationSection>;
}
