import { describe, expect, it } from "vitest";

import { describeAction, renderPreview } from "../entry.tsx";
import { hostexecPreviews } from "./requests.tsx";

const VALID_ARGS = {
  host: "wyrm2",
  run_as: "agentydragon",
  cmd: ["rg", "-n", "TODO", "src/"],
  max_bytes: 100_000,
  timeout_ms: 30_000,
  cwd: null,
};

describe("hostexecPreviews", () => {
  it("renders hostexec_run in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderPreview(hostexecPreviews.hostexec_run, VALID_ARGS, variant)).not.toBeNull();
    }
  });

  it("renders when cwd is omitted", () => {
    const { cwd: _cwd, ...withoutCwd } = VALID_ARGS;
    expect(renderPreview(hostexecPreviews.hostexec_run, withoutCwd, "detailed")).not.toBeNull();
  });

  it("describes the action with the host and run-as user", () => {
    expect(describeAction(hostexecPreviews.hostexec_run, VALID_ARGS)?.text).toBe(
      "hostexec: Run on wyrm2 as agentydragon"
    );
  });

  it("returns null when cmd is empty", () => {
    expect(renderPreview(hostexecPreviews.hostexec_run, { ...VALID_ARGS, cmd: [] }, "detailed")).toBeNull();
  });

  it("returns null when args don't match the tool's schema", () => {
    const { host: _host, ...withoutHost } = VALID_ARGS;
    expect(renderPreview(hostexecPreviews.hostexec_run, withoutHost, "detailed")).toBeNull();
  });
});
