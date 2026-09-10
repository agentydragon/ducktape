import { afterEach, describe, expect, it, vi } from "vitest";

import { api, fetchGrants, type GrantPrincipal } from "./client";

afterEach(() => vi.restoreAllMocks());

describe("fetchGrants", () => {
  it("sends an exact declared principal as the list filter", async () => {
    const get = vi.spyOn(api, "GET").mockResolvedValue({ data: { grants: [] } } as never);
    const principal = {
      kind: "agent",
      agent_id: "00000000-0000-4000-8000-000000000002",
    } satisfies GrantPrincipal;

    await fetchGrants(principal);

    expect(get).toHaveBeenCalledWith("/api/grants", {
      params: { query: { principal: JSON.stringify(principal) } },
    });
  });
});
