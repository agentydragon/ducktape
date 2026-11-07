import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import { actionArguments } from "../../presentation";
import { fileCount, pushedFilesSummary, zPushFilesArguments } from "../../schemas/github/push_files";
import type { ActionNotificationParts } from "../types";

export function pushFilesNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = zPushFilesArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;
  const { owner, repo, branch, message, files } = parsed.data;
  return {
    actionTitle: `Push ${fileCount(files.length)} to ${branch} in ${owner}/${repo}`,
    text: [message, ...pushedFilesSummary(files)].join("\n"),
  };
}
