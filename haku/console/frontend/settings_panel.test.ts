import { describe, expect, it } from "vitest";

import type { DeploymentInfo } from "./client.ts";
import { deploymentVersions } from "./settings_panel.tsx";

function deployment(server: string | null, frontend: string | null): DeploymentInfo {
  const image = (commit: string | null) => ({
    image_tag: commit ? `devel-20260713020000-${commit}` : null,
    source_commit: commit,
    source_commit_url: commit ? `https://github.com/agentydragon/ducktape/commit/${commit}` : null,
  });
  return { server: image(server), frontend: image(frontend) };
}

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
