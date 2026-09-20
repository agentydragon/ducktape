// Page-level harness for visual regression testing.
// Renders full pages with mock data to verify overall layout and navigation.

import { createRoot } from "react-dom/client";
import type { ComponentType } from "react";
import { MantineProvider } from "@mantine/core";

import { SCENARIOS } from "./scenarios.mjs";
import "../../src/app.css";

// Import React components rendered by the scenarios.
import DefinitionDetail from "../../src/components/DefinitionDetail";
import FileViewer from "../../src/components/FileViewer";
import LLMRequestViewer from "../../src/components/LLMRequestViewer";
import DistributionChart from "../../src/components/stats/DistributionChart";
import CoverageHeatmap from "../../src/components/stats/CoverageHeatmap";
import OccurrenceStats from "../../src/components/stats/OccurrenceStats";
import RunsBrowser from "../../src/components/RunsBrowser";
import RunDetail from "../../src/components/RunDetail";
import SnapshotDetailPage from "../../src/pages/SnapshotDetailPage";

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

type ScenarioPage = {
  component: ComponentType<any>;
  props: Record<string, unknown>;
  wrapperClassName?: string;
};

const pages: Record<string, ScenarioPage> = {
  // Definition detail page - shows stats, CLI command, recall table.
  DefinitionDetail: {
    component: DefinitionDetail,
    props: {
      data: definitionData,
    },
  },

  // File viewer with full annotations - TP, FP, critique issues, grading.
  FileViewerAnnotated: {
    component: FileViewer,
    props: {
      file: fileContent,
      tps,
      fps,
      critiqueIssues,
      gradingEdges,
      snapshotSlug: "test-snapshot",
    },
  },

  // File viewer with just ground truth (no critique).
  FileViewerGroundTruth: {
    component: FileViewer,
    props: {
      file: fileContent,
      tps,
      fps,
      snapshotSlug: "test-snapshot",
    },
  },

  // LLM request viewer with multiple requests including errors (first request expanded).
  LLMRequests: {
    component: LLMRequestViewer,
    props: {
      requests: llmRequests,
      initialExpanded: [1],
    },
  },

  // LLM request viewer showing an expanded tool-call turn.
  LLMRequestsToolCall: {
    component: LLMRequestViewer,
    props: {
      requests: llmRequests,
      initialExpanded: [2],
    },
  },

  // The two-up layout matches the overview page's stats grid so the chart width is representative.
  DistributionChartRecall: {
    component: DistributionChart,
    props: {
      values: recallDistribution,
      title: "Max Recall Distribution (Valid Examples)",
      numBuckets: 10,
      valueFormat: (value: number) => `${(value * 100).toFixed(1)}%`,
      color: "rgb(59, 130, 246)",
    },
    wrapperClassName: "grid grid-cols-2 gap-4",
  },

  DistributionChartTP: {
    component: DistributionChart,
    props: {
      values: tpCountDistribution,
      title: "True Positive Count Distribution (Valid Examples)",
      numBuckets: 8,
      valueFormat: (value: number) => value.toFixed(0),
      color: "rgb(34, 197, 94)",
    },
    wrapperClassName: "grid grid-cols-2 gap-4",
  },

  CoverageHeatmap: {
    component: CoverageHeatmap,
    props: {
      definitions: coverageDefinitions,
      examples: coverageExamples,
      cells: coverageCells,
    },
  },

  OccurrenceStatsTable: {
    component: OccurrenceStats,
    props: {
      occurrences: occurrenceStats,
    },
  },

  // Runs browser with mock data (no API calls).
  RunsBrowser: {
    component: RunsBrowser,
    props: {
      initialRuns: runs,
      initialTotalCount: runs.length,
    },
  },

  // Critic run with critique-vs-ground-truth file viewer.
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

  // Snapshot detail page with ground truth (files tab, TPs, FPs).
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
// not the other means a scenario that is never rendered, or one the runner asks for and cannot get.
const declared = new Set(SCENARIOS);
const mountable = new Set(Object.keys(pages));
const missing = [...declared].filter((name) => !mountable.has(name));
const unswept = [...mountable].filter((name) => !declared.has(name));
if (missing.length || unswept.length) {
  throw new Error(
    `scenarios.mjs and harness pages disagree: ${JSON.stringify({ missingFromHarness: missing, missingFromScenarios: unswept })}`
  );
}

const params = new URLSearchParams(window.location.search);
const pageName = params.get("page");
const app = document.getElementById("app");
if (!app) throw new Error("missing #app");
const root = createRoot(app);

if (!pageName) {
  root.render(
    <main style={{ fontFamily: "system-ui", padding: 20 }}>
      <h1>Visual Test Harness</h1>
      <p>Available page scenarios:</p>
      <ul>
        {Object.keys(pages).map((name) => (
          <li key={name}>
            <a href={`?page=${encodeURIComponent(name)}`}>{name}</a>
          </li>
        ))}
      </ul>
    </main>
  );
} else {
  const page = pages[pageName];
  if (!page) {
    root.render(<div style={{ color: "red", padding: 20 }}>Unknown page: {pageName}</div>);
  } else {
    const ScenarioComponent = page.component;
    const shot = (
      <div id="shot">
        <ScenarioComponent {...(page.props as any)} />
      </div>
    );
    root.render(
      <MantineProvider defaultColorScheme="auto">
        {page.wrapperClassName ? <div className={page.wrapperClassName}>{shot}</div> : shot}
      </MantineProvider>
    );
  }
}
