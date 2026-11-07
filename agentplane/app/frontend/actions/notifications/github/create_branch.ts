import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import { actionArguments } from "../../presentation";
import { zCreateBranchArguments } from "../../schemas/github/create_branch";
import type { ActionNotificationParts } from "../types";

export function createBranchNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = zCreateBranchArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;
  const { owner, repo, branch, from_branch } = parsed.data;
  return {
    actionTitle: `Create branch ${branch} from ${from_branch ?? "the default branch"} in ${owner}/${repo}`,
    text: request.description ?? "Action requires approval",
  };
}
