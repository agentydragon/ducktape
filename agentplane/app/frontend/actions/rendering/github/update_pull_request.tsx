import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { TextBlock } from "../../../text_block";
import {
  pullRequestUpdateChanges,
  pullRequestUpdateVerb,
  type UpdatePullRequestArguments,
  zUpdatePullRequestArguments,
} from "../../schemas/github/update_pull_request";

function UpdatePullRequestLabel({ args }: { args: UpdatePullRequestArguments }): JSX.Element {
  const changes = pullRequestUpdateChanges(args);
  return (
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      {pullRequestUpdateVerb(args)}{" "}
      <Code>
        {args.owner}/{args.repo}#{args.pullNumber}
      </Code>
      {changes.length > 0 && `: ${changes.join(", ")}`}
    </Text>
  );
}

// The label names every change; only the new text it replaces is left to show.
function UpdatePullRequestCollapsed({ args }: { args: UpdatePullRequestArguments }): JSX.Element | null {
  return args.title === undefined && !args.body ? null : (
    <Stack gap={2}>
      {args.title !== undefined && (
        <Text size="sm" lineClamp={2} style={{ overflowWrap: "anywhere" }}>
          New title: {args.title}
        </Text>
      )}
      {args.body && (
        <Text size="sm" lineClamp={2} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {args.body}
        </Text>
      )}
    </Stack>
  );
}

function UpdatePullRequest({ args }: { args: UpdatePullRequestArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      {args.title !== undefined && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          New title: <strong>{args.title}</strong>
        </Text>
      )}
      {args.body && <TextBlock text={args.body} heading="New description" format="prose" clamp={false} />}
    </Stack>
  );
}

export const updatePullRequestLabel: ArgumentsPreview = definePreview(
  zUpdatePullRequestArguments,
  UpdatePullRequestLabel
);
export const updatePullRequestCollapsed: ArgumentsPreview = definePreview(
  zUpdatePullRequestArguments,
  UpdatePullRequestCollapsed
);
export const updatePullRequestPane: ArgumentsPreview = definePreview(zUpdatePullRequestArguments, UpdatePullRequest);

export function offersCardDecisionButtonsUpdatePullRequest(args: unknown): boolean {
  const parsed = zUpdatePullRequestArguments.safeParse(args);
  return parsed.success && (parsed.data.state !== undefined || pullRequestUpdateChanges(parsed.data).length > 0);
}

export const updatePullRequestPresentation: ActionPresentation = {
  fallbackLabel: "Update GitHub pull request",
  label: updatePullRequestLabel,
  pane: { collapsed: updatePullRequestCollapsed, opened: updatePullRequestPane },
  details: { arguments: updatePullRequestPane },
  offersCardDecisionButtons: offersCardDecisionButtonsUpdatePullRequest,
};
