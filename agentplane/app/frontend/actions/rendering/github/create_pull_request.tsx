import type { JSX } from "react";
import { Badge, Code, Group, Stack, Text } from "@mantine/core";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { TextBlock } from "../../../text_block";
import {
  type CreatePullRequestArguments,
  pullRequestOptions,
  zCreatePullRequestArguments,
} from "../../schemas/github/create_pull_request";

function CreatePullRequestLabel({ args }: { args: CreatePullRequestArguments }): JSX.Element {
  return (
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      Open PR into <Code>{args.base}</Code> of{" "}
      <Code>
        {args.owner}/{args.repo}
      </Code>
      : {args.title}
    </Text>
  );
}

function HeadAndOptions({ args }: { args: CreatePullRequestArguments }): JSX.Element {
  return (
    <Group gap={4} wrap="wrap">
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        from <Code>{args.head}</Code>
      </Text>
      {pullRequestOptions(args).map((option) => (
        <Badge key={option} variant="light" color="gray" size="sm" tt="none" fw={500} maw="100%">
          {option}
        </Badge>
      ))}
    </Group>
  );
}

// Keep the first lines of the PR description visible on the compact card.
function CreatePullRequestCollapsed({ args }: { args: CreatePullRequestArguments }): JSX.Element {
  return (
    <Stack gap={2}>
      <HeadAndOptions args={args} />
      {args.body && (
        <Text size="sm" lineClamp={2} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {args.body}
        </Text>
      )}
    </Stack>
  );
}

function CreatePullRequest({ args }: { args: CreatePullRequestArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <HeadAndOptions args={args} />
      {args.body && <TextBlock text={args.body} heading="Description" format="prose" clamp={false} />}
    </Stack>
  );
}

export function offersCardDecisionButtonsPullRequest(args: unknown): boolean {
  return zCreatePullRequestArguments.safeParse(args).success;
}

export const createPullRequestLabel: ArgumentsPreview = definePreview(
  zCreatePullRequestArguments,
  CreatePullRequestLabel
);
export const createPullRequestCollapsed: ArgumentsPreview = definePreview(
  zCreatePullRequestArguments,
  CreatePullRequestCollapsed
);
export const createPullRequestPane: ArgumentsPreview = definePreview(zCreatePullRequestArguments, CreatePullRequest);

export const createPullRequestPresentation: ActionPresentation = {
  fallbackLabel: "Open GitHub pull request",
  label: createPullRequestLabel,
  pane: { collapsed: createPullRequestCollapsed, opened: createPullRequestPane },
  details: { arguments: createPullRequestPane },
  offersCardDecisionButtons: offersCardDecisionButtonsPullRequest,
};
