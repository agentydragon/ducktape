import type { ActionRequestView } from "../../types";
import { pullRequestOptions, zCreatePullRequestArguments } from "../../schemas/github/create_pull_request";
import type { ActionNotificationParts } from "../types";

export function createPullRequestNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = zCreatePullRequestArguments.safeParse(request.arguments);
  if (!parsed.success) return null;
  const args = parsed.data;
  return {
    actionTitle: `Open PR into ${args.base} of ${args.owner}/${args.repo}: ${args.title}`,
    text: [[`from ${args.head}`, ...pullRequestOptions(args)].join(" · "), ...(args.body ? [args.body] : [])].join(
      "\n"
    ),
  };
}
