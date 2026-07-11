import { describe, expect, it } from "vitest";

import { HOME_PATH, SETTINGS_PATH, TOOL_CALLS_PATH, viewForPathname } from "./routing.ts";

describe("viewForPathname", () => {
  it("maps each console path to its view and everything else to the embed", () => {
    expect(viewForPathname(TOOL_CALLS_PATH)).toBe("toolCalls");
    expect(viewForPathname(SETTINGS_PATH)).toBe("settings");
    expect(viewForPathname(HOME_PATH)).toBe("embed");
    // Any unknown path degrades to the embed shell, not a blank page.
    expect(viewForPathname("/something-else")).toBe("embed");
    // Only the exact path matches — a nested path is not the view.
    expect(viewForPathname(`${TOOL_CALLS_PATH}/extra`)).toBe("embed");
  });
});
