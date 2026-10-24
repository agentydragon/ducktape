import type { ActionRequestView } from "../../types";
import { fileCount, pushedFilesSummary, zPushFilesArguments } from "../../schemas/github/push_files";
import type { ActionNotificationParts } from "../types";

export function pushFilesNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = zPushFilesArguments.safeParse(request.arguments);
  if (!parsed.success) return null;
  const { owner, repo, branch, message, files } = parsed.data;
  return {
    actionTitle: `Push ${fileCount(files.length)} to ${branch} in ${owner}/${repo}`,
    text: [message, ...pushedFilesSummary(files)].join("\n"),
  };
}
