import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { useAsyncResource, type AsyncResource } from "./async_resource";
import type { DeploymentInfo } from "./client";
import type { IndexState } from "./mcp_status_client";
import { deploymentVersions, indexStatusDisplay, settingsTabFromSearch } from "./settings_panel";

function deployment(server: string | null, frontend: string | null): DeploymentInfo {
  const image = (commit: string | null) => ({
    image_tag: commit ? `devel-20260713020000-${commit}` : null,
    source_commit: commit,
    source_commit_url: commit ? `https://github.com/agentydragon/ducktape/commit/${commit}` : null,
  });
  return { server: image(server), frontend: image(frontend) };
}

describe("settingsTabFromSearch", () => {
  it("opens MCP servers by default", () => {
    expect(settingsTabFromSearch("")).toBe("mcp");
  });

  it("restores a linked tab", () => {
    expect(settingsTabFromSearch("?tab=nodes")).toBe("nodes");
    expect(settingsTabFromSearch("?tab=grants")).toBe("grants");
  });

  it("falls back safely for unknown tabs", () => {
    expect(settingsTabFromSearch("?tab=obsolete")).toBe("mcp");
  });
});

describe("settings resources", () => {
  it("keeps authoritative data when stale loads settle or refresh fails", async () => {
    let settle: (value: string) => void = () => undefined;
    const pending = new Promise<string>((resolve) => (settle = resolve));
    const load = vi.fn().mockReturnValueOnce(pending).mockRejectedValueOnce(new Error("offline"));
    const resource: { current: AsyncResource<string> | null } = { current: null };
    const root = createRoot(document.createElement("div"));
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    function Harness() {
      resource.current = useAsyncResource(load);
      return null;
    }
    act(() => root.render(createElement(Harness)));
    await vi.waitFor(() => expect(load).toHaveBeenCalledOnce());
    act(() => resource.current?.update("authoritative"));
    await act(async () => {
      settle("stale");
      await pending;
    });
    expect(resource.current?.data).toBe("authoritative");
    act(() => resource.current?.refresh());
    await vi.waitFor(() => expect(resource.current?.error).toBe("offline"));
    expect(resource.current?.data).toBe("authoritative");
    act(() => root.unmount());
  });
});

describe("deploymentVersions", () => {
  it("collapses matching server and web commits", () => {
    expect(deploymentVersions(deployment("83da566", "83da566"))).toEqual([
      expect.objectContaining({ label: "Deployed", image: expect.objectContaining({ source_commit: "83da566" }) }),
    ]);
  });

  it("exposes rollout skew", () => {
    expect(
      deploymentVersions(deployment("83da566", "bfad4bf")).map(({ label, image }) => [label, image.source_commit])
    ).toEqual([
      ["Server", "83da566"],
      ["Web", "bfad4bf"],
    ]);
  });

  it("omits unavailable metadata", () => {
    expect(deploymentVersions(deployment(null, null))).toEqual([]);
  });
});

describe("indexStatusDisplay", () => {
  const git = (indexed_commit: string | null, remote_commit: string | null): IndexState => ({
    index_type: "git",
    index_id: "ducktape",
    indexed_commit,
    remote_commit,
    remote_seen_at: null,
    branch: "devel",
    indexed_at: null,
    files: 1,
    chunks: 2,
    embedded_chunks: 2,
    pending_chunks: 0,
    superseded_chunks: 0,
  });

  it("distinguishes current, behind, and not-yet-built Git indexes", () => {
    expect(indexStatusDisplay(git("abc", "abc")).label).toBe("Current");
    expect(indexStatusDisplay(git("abc", "def")).label).toBe("Behind");
    expect(indexStatusDisplay(git(null, "def")).label).toBe("Not indexed");
  });

  it("reports pending chat work", () => {
    expect(
      indexStatusDisplay({
        index_type: "chat",
        index_id: "console-chats",
        sessions: 12,
        chunks: 30,
        stale_sessions: 1,
        unindexed_messages: 3,
        lag_seconds: 42,
        last_indexed_at: null,
        embedded_chunks: 27,
        pending_chunks: 3,
        superseded_chunks: 0,
      }).label
    ).toBe("Catching up");
  });
});
