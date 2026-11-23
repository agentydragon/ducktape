import { Badge } from "@mantine/core";

import type { AgentRunStatus, RunInfo } from "../lib/schema";

const colors: Partial<Record<AgentRunStatus, string>> = {
  in_progress: "blue",
  exited: "green",
  timed_out: "yellow",
  startup_failed: "red",
};

export function formatStatus(status: string): string {
  return status.replaceAll("_", " ");
}

export default function RunStatusBadge({ run }: { run: Pick<RunInfo, "status" | "container_exit_code"> }) {
  return (
    <Badge color={colors[run.status] ?? "gray"} variant="light" tt="capitalize">
      {formatStatus(run.status)}
      {run.status === "exited" && run.container_exit_code != null ? ` (${run.container_exit_code})` : ""}
    </Badge>
  );
}
