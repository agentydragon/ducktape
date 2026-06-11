// Shared status utilities for agent runs
import type { AgentRunStatus } from "./api/client";

// Re-export for convenience
export type { AgentRunStatus };

/** Returns Tailwind classes for status badge styling */
export function getStatusColor(status: AgentRunStatus): string {
  switch (status) {
    case "in_progress":
      return "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200";
    case "exited":
      return "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200";
    case "timed_out":
      return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200";
    default:
      return "bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200";
  }
}

/** Formats status for display (replaces underscores with spaces) */
export function formatStatus(status: AgentRunStatus): string {
  return status.replace(/_/g, " ");
}
