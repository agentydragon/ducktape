import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "../../../../../../util/frontend/clamped_block";
import { Disclosure } from "../../../../../../util/frontend/disclosure";
import { OperationSection } from "../../../operation_section";
import { TextBlock } from "../../../text_block";
import { CodeBlock, type Language } from "../../../../../../util/frontend/code_block";
import {
  fileCount,
  type PushedFile,
  pushedFileSize,
  pushedFilesSummary,
  type PushFilesArguments,
  zPushFilesArguments,
} from "../../schemas/github/push_files";

const LANGUAGE_BY_EXTENSION: ReadonlyMap<string, Language> = new Map([
  ["bash", "bash"],
  ["cjs", "javascript"],
  ["go", "go"],
  ["js", "javascript"],
  ["json", "json"],
  ["mjs", "javascript"],
  ["nix", "nix"],
  ["proto", "protobuf"],
  ["py", "python"],
  ["rs", "rust"],
  ["sh", "bash"],
  ["ts", "typescript"],
  ["tsx", "typescript"],
  ["yaml", "yaml"],
  ["yml", "yaml"],
]);

function extension(path: string): string {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  return dot <= 0 ? "" : name.slice(dot + 1).toLowerCase();
}

function PushFilesLabel({ args }: { args: PushFilesArguments }): JSX.Element {
  return (
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      Push {fileCount(args.files.length)} to <Code>{args.branch}</Code> in{" "}
      <Code>
        {args.owner}/{args.repo}
      </Code>
    </Text>
  );
}

function PushFilesCollapsed({ args }: { args: PushFilesArguments }): JSX.Element {
  return (
    <Stack gap={2}>
      <Text size="sm" lineClamp={2} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        {args.message}
      </Text>
      {pushedFilesSummary(args.files).map((line) => (
        <Text key={line} size="xs" ff="monospace" lineClamp={2} style={{ overflowWrap: "anywhere" }}>
          {line}
        </Text>
      ))}
    </Stack>
  );
}

/** One pushed file, closed until opened: Markdown as prose, code highlighted by its extension. */
function PushedFileBlock({ file }: { file: PushedFile }): JSX.Element {
  const fileExtension = extension(file.path);
  return (
    <OperationSection surface="framed">
      <Disclosure
        summary={
          <Text size="sm" ff="monospace" style={{ overflowWrap: "anywhere" }}>
            {file.path}
            <Text span size="sm" c="dimmed" ff="var(--mantine-font-family)">
              {` · ${pushedFileSize(file)}`}
            </Text>
          </Text>
        }
      >
        <ClampedBlock
          maxHeightRem={TEXT_MAX_HEIGHT_REM}
          lines={lineCount(file.content)}
          label={file.path}
          stickyCollapse={false}
        >
          <CodeBlock
            text={file.content}
            language={LANGUAGE_BY_EXTENSION.get(fileExtension)}
            font={fileExtension === "md" ? "prose" : "monospace"}
            surface="plain"
            releaseWhenFar
          />
        </ClampedBlock>
      </Disclosure>
    </OperationSection>
  );
}

function PushFiles({ args }: { args: PushFilesArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <TextBlock text={args.message} heading="Commit message" format="prose" clamp={false} />
      {args.files.map((file, index) => (
        // A push may list the same path twice; position is its identity.
        <PushedFileBlock key={index} file={file} />
      ))}
    </Stack>
  );
}

export const pushFilesLabel: ArgumentsPreview = definePreview(zPushFilesArguments, PushFilesLabel);
export const pushFilesCollapsed: ArgumentsPreview = definePreview(zPushFilesArguments, PushFilesCollapsed);
export const pushFilesPane: ArgumentsPreview = definePreview(zPushFilesArguments, PushFiles);

export function offersCardDecisionButtonsPushFiles(args: unknown): boolean {
  return zPushFilesArguments.safeParse(args).success;
}

export const pushFilesPresentation: ActionPresentation = {
  fallbackLabel: "Push files to GitHub",
  label: pushFilesLabel,
  pane: { collapsed: pushFilesCollapsed, opened: pushFilesPane },
  details: { arguments: pushFilesPane },
  offersCardDecisionButtons: offersCardDecisionButtonsPushFiles,
};
