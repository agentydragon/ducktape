import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry.tsx";
import { hostexecResultPreviews } from "./responses.tsx";

describe("hostexecResultPreviews", () => {
  it("renders a successful exit, in both variants", () => {
    const result = { exit: { kind: "exited", exit_code: 0 }, stdout: "hello\nworld\n", stderr: "", duration_ms: 120 };
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderResultPreview(hostexecResultPreviews.bash, result, variant)).not.toBeNull();
    }
  });

  it("renders a nonzero exit with stderr", () => {
    const result = {
      exit: { kind: "exited", exit_code: 1 },
      stdout: "",
      stderr: "command not found\n",
      duration_ms: 50,
    };
    expect(renderResultPreview(hostexecResultPreviews.bash, result, "detailed")).not.toBeNull();
  });

  it("renders a killed process", () => {
    const result = { exit: { kind: "killed", signal: 9 }, stdout: "", stderr: "", duration_ms: 30_000 };
    expect(renderResultPreview(hostexecResultPreviews.bash, result, "detailed")).not.toBeNull();
  });

  it("renders a timed-out process", () => {
    const result = { exit: { kind: "timed_out" }, stdout: "partial output", stderr: "", duration_ms: 30_000 };
    expect(renderResultPreview(hostexecResultPreviews.bash, result, "detailed")).not.toBeNull();
  });

  it("renders a truncated stream", () => {
    const result = {
      exit: { kind: "exited", exit_code: 0 },
      stdout: { truncated_text: "first 100000 bytes", total_bytes: 5_000_000 },
      stderr: "",
      duration_ms: 200,
    };
    expect(renderResultPreview(hostexecResultPreviews.bash, result, "detailed")).not.toBeNull();
  });

  it("returns null when the payload doesn't match the result schema", () => {
    expect(renderResultPreview(hostexecResultPreviews.bash, { exit: { kind: "bogus" } }, "detailed")).toBeNull();
  });
});
