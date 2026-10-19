import type { JSX } from "react";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { Code, Stack, Text } from "@mantine/core";

import { definePreview, type ArgumentsPreview } from "../entry";
import { TextBlock } from "../../../text_block";

// Inline card decisions are limited to calls with no description; a compact preview never stands in
// for reviewing authored text, which remains available in the full argument view.
const createArgs = {
  owner: z.string().min(1),
  repo: z.string().min(1),
  title: z.string().min(1),
  head: z.string().min(1),
  base: z.string().min(1),
  body: z.string().optional(),
  draft: z.boolean().optional(),
  maintainer_can_modify: z.boolean().optional(),
  reviewers: z.array(z.string().min(1)).optional(),
};
const createPullRequest = z.strictObject(createArgs);
const approvablePullRequest = z.strictObject({
  ...createArgs,
  title: z.string().min(1).max(120),
  body: z.literal("").optional(),
  reviewers: z.array(z.string().min(1)).max(4).optional(),
});

function CreatePullRequest({ args }: { args: z.infer<typeof createPullRequest> }): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        Create{" "}
        <Code>
          {args.owner}/{args.repo}
        </Code>{" "}
        pull request “{args.title}” from <Code>{args.head}</Code> into <Code>{args.base}</Code>.
      </Text>
      {args.body && <TextBlock text={args.body} heading="Description" format="prose" clamp={false} />}
      {args.draft === true && <Text size="sm">Create as a draft.</Text>}
      {args.maintainer_can_modify !== undefined && (
        <Text size="sm">
          {args.maintainer_can_modify
            ? "Allow maintainers to edit the branch."
            : "Do not allow maintainers to edit the branch."}
        </Text>
      )}
      {args.reviewers !== undefined && args.reviewers.length > 0 && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Request review from {args.reviewers.join(", ")}.
        </Text>
      )}
    </Stack>
  );
}

function CreatePullRequestCollapsed({ args }: { args: z.infer<typeof createPullRequest> }): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        Open{" "}
        <Code>
          {args.owner}/{args.repo}
        </Code>{" "}
        pull request “{args.title}” from <Code>{args.head}</Code> into <Code>{args.base}</Code>.
      </Text>
      {args.body && (
        <Text size="sm" lineClamp={2} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          Description: {args.body}
        </Text>
      )}
      {args.draft === true && <Text size="sm">Draft pull request.</Text>}
      {args.reviewers !== undefined && args.reviewers.length > 0 && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Request review from {args.reviewers.join(", ")}.
        </Text>
      )}
      {args.maintainer_can_modify !== undefined && (
        <Text size="sm">
          {args.maintainer_can_modify
            ? "Allow maintainers to edit the branch."
            : "Do not allow maintainers to edit the branch."}
        </Text>
      )}
    </Stack>
  );
}

function CreatePullRequestLabel({ args }: { args: z.infer<typeof createPullRequest> }): JSX.Element {
  void args;
  return (
    <Text size="sm" fw={600}>
      Create pull request
    </Text>
  );
}

export const createPullRequestLabel: ArgumentsPreview = definePreview(createPullRequest, CreatePullRequestLabel);
export const createPullRequestCollapsed: ArgumentsPreview = definePreview(
  createPullRequest,
  CreatePullRequestCollapsed
);
export const createPullRequestPane: ArgumentsPreview = definePreview(createPullRequest, CreatePullRequest);
export function offersCardDecisionButtonsPullRequest(args: unknown): boolean {
  return approvablePullRequest.safeParse(args).success;
}

export const createPullRequestPresentation: ActionPresentation = {
  label: createPullRequestLabel,
  pane: { collapsed: createPullRequestCollapsed, opened: createPullRequestPane },
  details: { arguments: createPullRequestPane },
  offersCardDecisionButtons: offersCardDecisionButtonsPullRequest,
};
