import type { ActionRequestView } from "../../types";
import {
  pullRequestUpdateChanges,
  pullRequestUpdateVerb,
  zUpdatePullRequestArguments,
} from "../../schemas/github/update_pull_request";
import type { ActionNotificationParts } from "../types";

export function updatePullRequestNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = zUpdatePullRequestArguments.safeParse(request.arguments);
  if (!parsed.success) return null;
  const args = parsed.data;
  const changes = pullRequestUpdateChanges(args);
  const content = [
    ...(args.title === undefined ? [] : [`New title: ${args.title}`]),
    ...(args.body ? [args.body] : []),
  ];
  return {
    actionTitle:
      `${pullRequestUpdateVerb(args)} ${args.owner}/${args.repo}#${args.pullNumber}` +
      (changes.length > 0 ? `: ${changes.join(", ")}` : ""),
    text: content.length > 0 ? content.join("\n") : (request.description ?? "Action requires approval"),
  };
}
