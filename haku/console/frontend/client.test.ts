import { afterEach, describe, expect, it, vi } from "vitest";

import { api, listAgents } from "./client";

afterEach(() => vi.restoreAllMocks());

describe("listAgents", () => {
  it("loads the operator's Agent catalog", async () => {
    const get = vi.spyOn(api, "GET").mockResolvedValue({ data: { agents: [], access_profiles: [] } } as never);

    await listAgents();

    expect(get).toHaveBeenCalledWith("/api/agent-enrollment/agents");
  });
});
