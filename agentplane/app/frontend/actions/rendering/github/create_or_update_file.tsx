import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";

import { TextBlock } from "../../../text_block";
import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { pushedFileSize } from "../../schemas/github/push_files";
import {
  type CreateOrUpdateFileArguments,
  zCreateOrUpdateFileArguments,
} from "../../schemas/github/create_or_update_file";

function CreateOrUpdateFileLabel({ args }: { args: CreateOrUpdateFileArguments }): JSX.Element {
  const verb = args.sha === undefined ? "Create" : "Update";
  return (
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      {verb} <Code>{args.path}</Code> on <Code>{args.branch}</Code> in{" "}
      <Code>
        {args.owner}/{args.repo}
      </Code>
    </Text>
  );
}

function FileWriteOptions({ args }: { args: CreateOrUpdateFileArguments }): JSX.Element | null {
  if (args.sha === undefined && args.allow_symlink_write === undefined) return null;
  return (
    <Stack gap={2}>
      {args.sha !== undefined && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Replace blob SHA: <Code>{args.sha}</Code>
        </Text>
      )}
      {args.allow_symlink_write !== undefined && (
        <Text size="sm">Allow symlink write: {args.allow_symlink_write ? "yes" : "no"}</Text>
      )}
    </Stack>
  );
}

function CreateOrUpdateFileCollapsed({ args }: { args: CreateOrUpdateFileArguments }): JSX.Element {
  return (
    <Stack gap={2}>
      <Text size="sm" lineClamp={2} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        Commit message: {args.message}
      </Text>
      <Text size="xs" ff="monospace" style={{ overflowWrap: "anywhere" }}>
        Content · {pushedFileSize({ path: args.path, content: args.content })}
      </Text>
      <Text size="sm" lineClamp={2} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        {args.content || "(empty file)"}
      </Text>
      <FileWriteOptions args={args} />
    </Stack>
  );
}

function CreateOrUpdateFile({ args }: { args: CreateOrUpdateFileArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <TextBlock text={args.message} heading="Commit message" format="prose" clamp={false} />
      <TextBlock text={args.content} heading={args.path} />
      <FileWriteOptions args={args} />
    </Stack>
  );
}

export const createOrUpdateFileLabel: ArgumentsPreview = definePreview(
  zCreateOrUpdateFileArguments,
  CreateOrUpdateFileLabel
);
export const createOrUpdateFileCollapsed: ArgumentsPreview = definePreview(
  zCreateOrUpdateFileArguments,
  CreateOrUpdateFileCollapsed
);
export const createOrUpdateFilePane: ArgumentsPreview = definePreview(zCreateOrUpdateFileArguments, CreateOrUpdateFile);

export function offersCardDecisionButtonsCreateOrUpdateFile(args: unknown): boolean {
  return zCreateOrUpdateFileArguments.safeParse(args).success;
}

export const createOrUpdateFilePresentation: ActionPresentation = {
  fallbackLabel: "Create or update a GitHub file",
  label: createOrUpdateFileLabel,
  pane: { collapsed: createOrUpdateFileCollapsed, opened: createOrUpdateFilePane },
  details: { arguments: createOrUpdateFilePane },
  offersCardDecisionButtons: offersCardDecisionButtonsCreateOrUpdateFile,
};
