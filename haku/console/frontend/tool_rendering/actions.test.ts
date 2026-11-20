import { describe, expect, it } from "vitest";

import { toolActionDescription } from "./actions";
import { HAKU_ROUTINE_SERVER_ID } from "./server_ids";

describe("toolActionDescription", () => {
  it("describes a Haku routine launch", () => {
    expect(toolActionDescription(HAKU_ROUTINE_SERVER_ID, "launch_routine", {})?.text).toBe("Haku: Start a new run");
  });

  it("leaves retired Gmail and Tana calls on the generic path", () => {
    expect(toolActionDescription("gmail", "threads_get", {})).toBeNull();
    expect(toolActionDescription("tana", "trash_node", { nodeId: "n1" })?.destructive).toBe(true);
  });
});
