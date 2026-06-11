// Re-export types from generated schema (Bazel: //props/frontend:generate_schema)
import type { components } from "./api/schema";

export type OverviewResponse = components["schemas"]["OverviewResponse"];
export type DefinitionRow = components["schemas"]["DefinitionRow"];
export type SplitScopeStats = components["schemas"]["SplitScopeStats"];
export type StatsWithCI = components["schemas"]["StatsWithCI"];
export type Split = components["schemas"]["Split"];
export type ExampleKind = components["schemas"]["ExampleKind"];
export type AgentRunStatus = components["schemas"]["AgentRunStatus"];

// Composed types from backend schema for file viewers
export type LineRange = components["schemas"]["LineRange"];
export type FileLocationInfo = components["schemas"]["FileLocationInfo"];
export type GradingEdgeInfo = components["schemas"]["GradingEdgeInfo"];

/**
 * Unified marker for issues displayed in FileViewer.
 * Combines parent issue info (id, rationale) with occurrence info (files, note).
 * Supports TPs, FPs, and optionally critique issues with grading edges.
 *
 * To get ranges for a specific file, use: allFiles.find(f => f.path === filePath)?.ranges
 */
export interface IssueMarker {
  kind: "tp" | "fp" | "critique";
  issueId: string;
  occurrenceId?: string;
  rationale: string;
  note?: string;
  allFiles: FileLocationInfo[];
  gradingEdges?: GradingEdgeInfo[];
}

// UI-specific types
export interface RunModalPrefill {
  definitionId?: string;
  split?: Split;
  kind?: ExampleKind;
}

export interface RunTrigger {
  definitionId: string;
  split: Split;
  kind: ExampleKind;
}
