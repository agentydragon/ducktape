// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { deferred } from "../../../../util/frontend/testing/deferred";
import { fetchSnapshotDetail, fetchSnapshotFile, fetchSnapshotTree } from "../lib/api/client";
import type { FileContentResponse, FileTreeResponse, SnapshotDetailResponse } from "../lib/schema";
import { createPropsQueryClient } from "../lib/queryClient";
import SnapshotDetailPage from "./SnapshotDetailPage";

vi.mock("../lib/api/client", () => ({
  fetchOccurrenceStats: vi.fn(),
  fetchSnapshotClusters: vi.fn(),
  fetchSnapshotDetail: vi.fn(),
  fetchSnapshotFile: vi.fn(),
  fetchSnapshotTree: vi.fn(),
}));

vi.mock("../components/FileTree", () => ({
  default: ({
    nodes,
    onFileClick,
  }: {
    nodes: { path: string; name: string }[];
    onFileClick: (path: string) => void;
  }) => (
    <div>
      {nodes.map((node) => (
        <button key={node.path} type="button" onClick={() => onFileClick(node.path)}>
          {node.name}
        </button>
      ))}
    </div>
  ),
}));

vi.mock("../components/FileViewer", () => ({
  default: ({ file }: { file: FileContentResponse }) => <div data-testid="selected-file">{file.path}</div>,
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const snapshot = {
  slug: "test/snapshot",
  split: "valid",
  created_at: "2026-01-01T00:00:00Z",
  true_positives: [],
  false_positives: [],
} satisfies SnapshotDetailResponse;

const tree = {
  tree: [
    { path: "first.ts", name: "first.ts", is_dir: false, tp_count: 0, fp_count: 0, children: null },
    { path: "second.ts", name: "second.ts", is_dir: false, tp_count: 0, fp_count: 0, children: null },
  ],
} satisfies FileTreeResponse;

function file(path: string): FileContentResponse {
  return { path, content: `contents of ${path}`, line_count: 1 };
}

let root: Root | null = null;
let queryClient: ReturnType<typeof createPropsQueryClient> | null = null;
let container: HTMLDivElement;

async function mount(seedData = true): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  queryClient = createPropsQueryClient();
  await act(async () => {
    root?.render(
      <QueryClientProvider client={queryClient!}>
        <MantineProvider env="test">
          <MemoryRouter>
            <SnapshotDetailPage
              slug="test/snapshot"
              initialSnapshot={seedData ? snapshot : undefined}
              initialTree={seedData ? tree : undefined}
            />
          </MemoryRouter>
        </MantineProvider>
      </QueryClientProvider>
    );
  });
}

async function clickFile(path: string): Promise<void> {
  const button = Array.from(container.querySelectorAll("button")).find((candidate) => candidate.textContent === path);
  if (!button) throw new Error(`Missing file button ${path}`);
  await act(async () => button.click());
}

async function flushQueryNotifications(): Promise<void> {
  await act(async () => new Promise<void>((resolve) => setTimeout(resolve, 0)));
}

async function unmount(): Promise<void> {
  await act(async () => root?.unmount());
  root = null;
  queryClient?.clear();
  queryClient = null;
}

beforeEach(() => vi.resetAllMocks());

afterEach(async () => {
  if (root) await unmount();
  container?.remove();
});

it("loads selected files lazily and keeps the latest selection when an older request finishes", async () => {
  const firstFile = deferred<FileContentResponse>();
  const secondFile = deferred<FileContentResponse>();
  vi.mocked(fetchSnapshotFile).mockImplementation((_slug, path) =>
    path === "first.ts" ? firstFile.promise : secondFile.promise
  );

  await mount();
  expect(fetchSnapshotDetail).not.toHaveBeenCalled();
  expect(fetchSnapshotTree).not.toHaveBeenCalled();
  expect(fetchSnapshotFile).not.toHaveBeenCalled();

  await clickFile("first.ts");
  const firstSignal = vi.mocked(fetchSnapshotFile).mock.calls[0]?.[2];
  expect(fetchSnapshotFile).toHaveBeenCalledWith("test/snapshot", "first.ts", expect.any(AbortSignal));

  await clickFile("second.ts");
  expect(firstSignal?.aborted).toBe(true);
  expect(fetchSnapshotFile).toHaveBeenCalledWith("test/snapshot", "second.ts", expect.any(AbortSignal));

  await act(async () => {
    secondFile.resolve(file("second.ts"));
    await secondFile.promise;
  });
  await flushQueryNotifications();
  expect(container.querySelector('[data-testid="selected-file"]')?.textContent).toBe("second.ts");

  await act(async () => {
    firstFile.resolve(file("first.ts"));
    await firstFile.promise;
  });
  await flushQueryNotifications();
  expect(container.querySelector('[data-testid="selected-file"]')?.textContent).toBe("second.ts");
});

it("shows either snapshot read failure while the sibling read is still pending", async () => {
  const pendingTree = deferred<FileTreeResponse>();
  vi.mocked(fetchSnapshotDetail).mockRejectedValue(new Error("snapshot unavailable"));
  vi.mocked(fetchSnapshotTree).mockReturnValue(pendingTree.promise);

  await mount(false);
  await flushQueryNotifications();

  expect(fetchSnapshotDetail).toHaveBeenCalledWith("test/snapshot", undefined, undefined, expect.any(AbortSignal));
  expect(fetchSnapshotTree).toHaveBeenCalledWith("test/snapshot", expect.any(AbortSignal));
  expect(container.textContent).toContain("snapshot unavailable");
  expect(container.textContent).not.toContain("Loading...");
});
