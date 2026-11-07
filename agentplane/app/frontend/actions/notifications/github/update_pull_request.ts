import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import { actionArguments } from "../../presentation";
import {
  pullRequestUpdateChanges,
  pullRequestUpdateVerb,
  zUpdatePullRequestArguments,
} from "../../schemas/github/update_pull_request";
import type { ActionNotificationParts } from "../types";

export function updatePullRequestNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = zUpdatePullRequestArguments.safeParse(actionArguments(request));
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
