import type { ExampleKind, GradingEdgeInfo, LocationAnchor, Split } from "./schema";

// Composed types from backend schema for file viewers
/**
 * Unified marker for issues displayed in FileViewer.
 * Combines parent issue info (id, rationale) with occurrence-level locations.
 * Supports TPs, FPs, and optionally critique issues with grading edges.
 */
export interface IssueMarker {
  kind: "tp" | "fp" | "critique";
  issueId: string;
  occurrenceId?: string;
  rationale: string;
  note?: string;
  allLocations: LocationAnchor[];
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
