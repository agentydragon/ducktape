import { describe, expect, it } from "vitest";

import { toolActionDescription } from "../actions";
import { renderPreview } from "../entry";
import { HOSTEXEC_SERVER_ID } from "../server_ids";
import { hostexecPreviews } from "./requests";

const VALID_ARGS = {
  host: "wyrm2",
  run_as: "agentydragon",
  cmd: "rg -n TODO src/",
  max_bytes: 100_000,
  timeout_ms: 30_000,
  cwd: null,
};

describe("hostexecPreviews", () => {
  it("renders bash in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderPreview(hostexecPreviews.bash, VALID_ARGS, variant)).not.toBeNull();
    }
  });

  it("renders when cwd is omitted", () => {
    const { cwd: _cwd, ...withoutCwd } = VALID_ARGS;
    expect(renderPreview(hostexecPreviews.bash, withoutCwd, "detailed")).not.toBeNull();
  });

  it("describes the action with the host and run-as user", () => {
    expect(toolActionDescription(HOSTEXEC_SERVER_ID, "bash", VALID_ARGS)?.text).toBe(
      "hostexec: Run on wyrm2 as agentydragon"
    );
  });

  it("returns null when cmd is empty", () => {
    expect(renderPreview(hostexecPreviews.bash, { ...VALID_ARGS, cmd: "" }, "detailed")).toBeNull();
  });

  it("returns null when args don't match the tool's schema", () => {
    const { host: _host, ...withoutHost } = VALID_ARGS;
    expect(renderPreview(hostexecPreviews.bash, withoutHost, "detailed")).toBeNull();
  });
});
