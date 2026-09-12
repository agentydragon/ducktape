// Page-level harness for visual regression testing
// Renders full pages with mock data to verify overall layout and navigation

import { mount } from "svelte";

import { SCENARIOS } from "./scenarios.mjs";
import "../../src/app.css";

// Import page components
import DefinitionDetail from "../../src/components/DefinitionDetail.svelte";
import FileViewer from "../../src/components/FileViewer.svelte";
import LLMRequestViewer from "../../src/components/LLMRequestViewer.svelte";
import DistributionChart from "../../src/components/stats/DistributionChart.svelte";
import CoverageHeatmap from "../../src/components/stats/CoverageHeatmap.svelte";
import OccurrenceStats from "../../src/components/stats/OccurrenceStats.svelte";
import RunsBrowser from "../../src/components/RunsBrowser.svelte";
import RunDetail from "../../src/components/RunDetail.svelte";
import SnapshotDetailPage from "../../src/pages/SnapshotDetailPage.svelte";

import { definitionData } from "./fixtures/definition";
import { critiqueIssues, fileContent, fps, gradingEdges, tps } from "./fixtures/files";
import { llmRequests } from "./fixtures/llm_requests";
import { criticRunDetail, runs } from "./fixtures/runs";
import { fileTree, snapshotDetail } from "./fixtures/snapshot";
import {
  coverageCells,
  coverageDefinitions,
  coverageExamples,
  occurrenceStats,
  recallDistribution,
  tpCountDistribution,
} from "./fixtures/stats";

const pages: Record<string, { component: any; props: Record<string, unknown>; wrapperClassName?: string }> = {
  // Definition detail page - shows stats, CLI command, recall table
  DefinitionDetail: {
    component: DefinitionDetail,
    props: {
      data: definitionData,
    },
  },

  // File viewer with full annotations - TP, FP, critique issues, grading
  FileViewerAnnotated: {
    component: FileViewer,
    props: {
      file: fileContent,
      tps: tps,
      fps: fps,
      critiqueIssues: critiqueIssues,
      gradingEdges: gradingEdges,
      snapshotSlug: "test-snapshot",
    },
  },

  // File viewer with just ground truth (no critique)
  FileViewerGroundTruth: {
    component: FileViewer,
    props: {
      file: fileContent,
      tps: tps,
      fps: fps,
      snapshotSlug: "test-snapshot",
    },
  },

  // LLM request viewer with multiple requests including errors (first request expanded)
  LLMRequests: {
    component: LLMRequestViewer,
    props: {
      requests: llmRequests,
      initialExpanded: [1],
    },
  },

  // LLM request viewer showing a tool-call turn expanded (input has function_call + result, output has function_call)
  LLMRequestsToolCall: {
    component: LLMRequestViewer,
    props: {
      requests: llmRequests,
      initialExpanded: [2],
    },
  },

  // Distribution chart - recall histogram. OverviewPage.svelte pairs two of these in
  // `grid grid-cols-2 gap-4`, so the chart's real rendered width is half that row — reproduce the
  // same grid here (rather than the harness's full width) so the shot matches production.
  DistributionChartRecall: {
    component: DistributionChart,
    props: {
      values: recallDistribution,
      title: "Max Recall Distribution (Valid Examples)",
      numBuckets: 10,
      valueFormat: (v: number) => `${(v * 100).toFixed(1)}%`,
      color: "rgb(59, 130, 246)",
    },
    wrapperClassName: "grid grid-cols-2 gap-4",
  },

  // Distribution chart - TP count histogram (see DistributionChartRecall re: grid width)
  DistributionChartTP: {
    component: DistributionChart,
    props: {
      values: tpCountDistribution,
      title: "True Positive Count Distribution (Valid Examples)",
      numBuckets: 8,
      valueFormat: (v: number) => `${v.toFixed(0)}`,
      color: "rgb(34, 197, 94)",
    },
    wrapperClassName: "grid grid-cols-2 gap-4",
  },

  // Coverage heatmap
  CoverageHeatmap: {
    component: CoverageHeatmap,
    props: {
      definitions: coverageDefinitions,
      examples: coverageExamples,
      cells: coverageCells,
    },
  },

  // Occurrence stats table
  OccurrenceStatsTable: {
    component: OccurrenceStats,
    props: {
      occurrences: occurrenceStats,
    },
  },

  // Runs browser with mock data (no API calls)
  RunsBrowser: {
    component: RunsBrowser,
    props: {
      initialRuns: runs,
      initialTotalCount: runs.length,
    },
  },

  // Run detail: critic run with critique-vs-ground-truth file viewer
  RunDetailCritic: {
    component: RunDetail,
    props: {
      runId: "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      initialRun: criticRunDetail,
      initialSnapshotDetail: snapshotDetail,
      initialFileContents: new Map([["src/auth/login.py", fileContent]]),
      initialLLMRequests: llmRequests,
    },
  },

  // Snapshot detail page with ground truth (files tab, TPs, FPs)
  SnapshotDetail: {
    component: SnapshotDetailPage,
    props: {
      slug: "vuln-app-v1",
      initialSnapshot: snapshotDetail,
      initialTree: fileTree,
    },
  },
};

// scenarios.mjs is what the sweep runs; `pages` is what this harness can mount. A name in one and
// not the other means a scenario that is never rendered, or one the runner asks for and cannot get
// -- both of which used to pass silently. Fail on the first scenario instead.
const declared = new Set(SCENARIOS);
const mountable = new Set(Object.keys(pages));
const missing = [...declared].filter((name) => !mountable.has(name));
const unswept = [...mountable].filter((name) => !declared.has(name));
if (missing.length || unswept.length) {
  throw new Error(
    `scenarios.mjs and harness pages disagree: ${JSON.stringify({ missingFromHarness: missing, missingFromScenarios: unswept })}`
  );
}

// Parse URL parameters
const params = new URLSearchParams(window.location.search);
const pageName = params.get("page");

const app = document.getElementById("app")!;

if (!pageName) {
  // Show available pages
  app.innerHTML = `
    <div style="font-family: system-ui; padding: 20px;">
      <h1>Visual Test Harness</h1>
      <p>Available page scenarios:</p>
      <ul>
        ${Object.keys(pages)
          .map((name) => `<li><a href="?page=${name}">${name}</a></li>`)
          .join("")}
      </ul>
    </div>
  `;
} else if (!pages[pageName]) {
  app.innerHTML = `<div style="color: red; padding: 20px;">Unknown page: ${pageName}</div>`;
} else {
  const { component, props, wrapperClassName } = pages[pageName];
  // #shot is the visual test's screenshot target (see visual-test-lib.mjs's required `element`
  // option): a plain pass-through div for a real full-page component, or — when wrapperClassName
  // reproduces a real production container (e.g. the two-up stats grid) — the actual grid cell, so
  // the shot's width matches how the component truly renders instead of the harness's full width.
  const shot = document.createElement("div");
  shot.id = "shot";
  if (wrapperClassName) {
    const wrapper = document.createElement("div");
    wrapper.className = wrapperClassName;
    wrapper.appendChild(shot);
    app.appendChild(wrapper);
  } else {
    app.appendChild(shot);
  }
  mount(component, {
    target: shot,
    props,
  });
}
