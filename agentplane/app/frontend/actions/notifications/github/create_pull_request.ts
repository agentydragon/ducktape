import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import { actionArguments } from "../../presentation";
import { pullRequestOptions, zCreatePullRequestArguments } from "../../schemas/github/create_pull_request";
import type { ActionNotificationParts } from "../types";

export function createPullRequestNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = zCreatePullRequestArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;
  const args = parsed.data;
  return {
    actionTitle: `Open PR into ${args.base} of ${args.owner}/${args.repo}: ${args.title}`,
    text: [[`from ${args.head}`, ...pullRequestOptions(args)].join(" · "), ...(args.body ? [args.body] : [])].join(
      "\n"
    ),
  };
}
