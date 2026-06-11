// Shared status utilities for agent runs
import type { AgentRunStatus } from "./api/client";

// Re-export for convenience
export type { AgentRunStatus };

/** Returns Tailwind classes for status badge styling */
export function getStatusColor(status: AgentRunStatus): string {
  switch (status) {
    case "in_progress":
      return "bg-blue-100 text-blue-800";
    case "completed":
      return "bg-green-100 text-green-800";
    case "max_turns_exceeded":
    case "context_length_exceeded":
      return "bg-yellow-100 text-yellow-800";
    case "reported_failure":
      return "bg-red-100 text-red-800";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

/** Formats status for display (replaces underscores with spaces) */
export function formatStatus(status: AgentRunStatus): string {
  return status.replace(/_/g, " ");
}
