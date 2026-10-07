import { describe, expect, it } from "vitest";

import type { DeploymentInfo } from "./client";
import { deploymentVersions, settingsTabFromSearch } from "./settings_panel";

function deployment(server: string | null, frontend: string | null): DeploymentInfo {
  const image = (commit: string | null) => ({
    image_tag: commit ? `devel-20260713020000-${commit}` : null,
    source_commit: commit,
    source_commit_url: commit ? `https://github.com/agentydragon/ducktape/commit/${commit}` : null,
  });
  return { server: image(server), frontend: image(frontend) };
}

describe("settingsTabFromSearch", () => {
  it("honors a known tab and falls back to Agents for any other", () => {
    expect(settingsTabFromSearch("?tab=grants")).toBe("grants");
    expect(settingsTabFromSearch("?tab=obsolete")).toBe("agents");
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
