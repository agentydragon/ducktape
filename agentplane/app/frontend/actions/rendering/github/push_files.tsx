import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { createTwoFilesPatch, OMIT_HEADERS } from "diff";
import { filesPushed, GitHubLink, repositoryHref } from "./receipts";

import { getGitHubPushFilesPreview } from "../../client";
import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "../../../../../../util/frontend/clamped_block";
import { Disclosure } from "../../../../../../util/frontend/disclosure";
import { OperationSection } from "../../../ui/operation_section";
import { TextBlock } from "../../../ui/text_block";
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

function PushedFileBlock({
  file,
  before,
  defaultOpen,
}: {
  file: PushedFile;
  before: string | undefined;
  defaultOpen: boolean;
}): JSX.Element {
  return (
    <OperationSection surface="framed">
      <Disclosure
        defaultOpen={defaultOpen}
        summary={
          <Text size="sm" ff="monospace" style={{ overflowWrap: "anywhere" }}>
            {file.path}
            <Text span size="sm" c="dimmed" ff="var(--mantine-font-family)">
              {` · ${pushedFileSize(file)}`}
            </Text>
          </Text>
        }
      >
        <PushedFileContent file={file} before={before} />
      </Disclosure>
    </OperationSection>
  );
}

function PushedFileContent({ file, before }: { file: PushedFile; before: string | undefined }): JSX.Element {
  const fileExtension = extension(file.path);
  const proposed = (
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
  );
  if (before === undefined) return proposed;

  const patch =
    before === file.content
      ? undefined
      : createTwoFilesPatch("before", "after", before, file.content, undefined, undefined, {
          headerOptions: OMIT_HEADERS,
          timeout: 50,
        });
  return (
    <Stack gap="xs">
      {patch === undefined ? (
        <Text size="sm" c="dimmed">
          {before === file.content ? "No text changes." : "This file's diff is too large to display."}
        </Text>
      ) : (
        <ClampedBlock maxHeightRem={TEXT_MAX_HEIGHT_REM} lines={lineCount(patch)} label="Diff" stickyCollapse={false}>
          <CodeBlock text={patch} language="diff" surface="plain" releaseWhenFar />
        </ClampedBlock>
      )}
      {patch === undefined && before !== file.content ? (
        proposed
      ) : (
        <Disclosure summary={<Text size="sm">Proposed contents</Text>}>{proposed}</Disclosure>
      )}
    </Stack>
  );
}

function PushFiles({ args, requestId }: { args: PushFilesArguments; requestId?: string }): JSX.Element {
  const query = useQuery({
    queryKey: ["github-push-files-preview", requestId],
    queryFn: ({ signal }) => getGitHubPushFilesPreview(requestId ?? "", signal),
    enabled: requestId !== undefined,
    gcTime: 0,
    staleTime: 0,
    retry: false,
  });
  const preview = query.isFetching || query.isError ? undefined : query.data;
  const baseline = new Map(preview?.files.map((file) => [file.path, file.content]));
  return (
    <Stack gap="xs">
      <TextBlock text={args.message} heading="Commit message" format="prose" clamp={false} />
      <Text size="sm" c="dimmed">
        Creates the branch from the default branch if needed.
      </Text>
      {requestId !== undefined && (
        <Text size="sm" c="dimmed" role="status">
          {query.isFetching ? (
            "Reading the target branch…"
          ) : preview?.baseCommitSha && baseline.size > 0 ? (
            <>
              Compared with{" "}
              <GitHubLink href={`${repositoryHref(args)}/commit/${encodeURIComponent(preview.baseCommitSha)}`}>
                <Code>{preview.baseCommitSha.slice(0, 7)}</Code>
              </GitHubLink>
              {baseline.size < args.files.length && ". Other files show proposed contents."}
            </>
          ) : (
            "Comparison unavailable. Files show proposed contents."
          )}
        </Text>
      )}
      {args.files.map((file, index) => (
        // A push may list the same path twice; position is its identity.
        <PushedFileBlock
          key={index}
          file={file}
          before={baseline.get(file.path)}
          defaultOpen={requestId !== undefined && args.files.length === 1}
        />
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
  details: { arguments: pushFilesPane, pendingPreviewOnly: true, result: filesPushed },
  offersCardDecisionButtons: offersCardDecisionButtonsPushFiles,
};
