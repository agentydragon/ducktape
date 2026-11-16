import { expect, it } from "vitest";

import {
  readSessionViewerSearch,
  sessionFilterPath,
  sessionKindsFromScope,
  sessionKindsPath,
  sessionPath,
  sessionVersionPath,
} from "./session-viewer-url";

it("defaults to main and unknown sessions when kind filters are absent", () => {
  expect(readSessionViewerSearch("")).toEqual({
    machineId: "",
    harness: "",
    kindScope: "main,unknown",
    versionId: null,
  });
  expect(sessionKindsFromScope("main,unknown")).toEqual(["main", "unknown"]);
});

it("normalizes repeated kind filters in the archive's display order", () => {
  expect(readSessionViewerSearch("?kind=unknown&kind=guardian&kind=guardian&kind=future").kindScope).toBe(
    "guardian,unknown"
  );
  expect(sessionKindsFromScope("guardian,unknown")).toEqual(["guardian", "unknown"]);
});

it("keeps an explicitly empty kind filter and reads the other selections", () => {
  expect(readSessionViewerSearch("?machine=thinkpad&harness=codex&kind=&version=capture-1")).toEqual({
    machineId: "thinkpad",
    harness: "codex",
    kindScope: "",
    versionId: "capture-1",
  });
  expect(sessionKindsFromScope("")).toEqual([]);
});

it("builds session links with encoded IDs and preserves filters while clearing the version", () => {
  expect(sessionPath("session/one", "?machine=thinkpad&kind=guardian&kind=main&version=old")).toBe(
    "/sessions/session%2Fone?machine=thinkpad&kind=guardian&kind=main"
  );
});

it("updates a filter, clears the selected version, and preserves the existing query order", () => {
  expect(sessionFilterPath("?machine=old&harness=claude&kind=guardian&version=old", "machine", "new")).toBe(
    "/?machine=new&harness=claude&kind=guardian"
  );
  expect(sessionFilterPath("?machine=old&version=old", "machine", "")).toBe("/?");
});

it("orders selected kinds canonically and preserves an explicit empty selection", () => {
  expect(sessionKindsPath("/sessions/one", "?kind=unknown&kind=main&machine=m", ["unknown", "main"])).toBe(
    "/sessions/one?machine=m&kind=main&kind=unknown"
  );
  expect(sessionKindsPath("/sessions/one", "?machine=m&version=v", [])).toBe("/sessions/one?machine=m&version=v&kind=");
});

it("sets or removes the requested version while preserving other query parameters", () => {
  expect(sessionVersionPath("/sessions/one", "?machine=m&version=old&kind=guardian", "new")).toBe(
    "/sessions/one?machine=m&version=new&kind=guardian"
  );
  expect(sessionVersionPath("/sessions/one", "?machine=m&version=old", "")).toBe("/sessions/one?machine=m");
});
