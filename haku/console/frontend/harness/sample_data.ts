import type { DeploymentInfo } from "../api/schema";

// Stable deployment metadata for the screenshot harness settings scene.

export const SAMPLE_DEPLOYMENT: DeploymentInfo = {
  server: {
    image_tag: "devel-20260713014452-83da566",
    source_commit: "83da566",
    source_commit_url: "https://github.com/agentydragon/ducktape/commit/83da566",
  },
  frontend: {
    image_tag: "devel-20260713015518-bfad4bf",
    source_commit: "bfad4bf",
    source_commit_url: "https://github.com/agentydragon/ducktape/commit/bfad4bf",
  },
};
