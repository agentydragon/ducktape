/**
 * Every visual scenario the harness can mount, in the order it declares them.
 *
 * The one list: harness.ts checks its `pages` against it, and tests/visual_runner.mjs sweeps it,
 * so BUILD names no scenario. Plain `.mjs` rather than `.ts` because both readers need it and only
 * one of them is compiled -- the harness is bundled by esbuild, the runner is run by node.
 */
export const SCENARIOS = [
  "DefinitionDetail",
  "FileViewerAnnotated",
  "FileViewerGroundTruth",
  "LLMRequests",
  "LLMRequestsToolCall",
  "DistributionChartRecall",
  "DistributionChartTP",
  "CoverageHeatmap",
  "OccurrenceStatsTable",
  "RunsBrowser",
  "RunDetailCritic",
  "SnapshotDetail",
];
