// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from "vitest";

import {
  CONSOLE_ROOT_PATH,
  HOME_PATH,
  rememberedEmbedPath,
  rememberEmbedPath,
  SETTINGS_PATH,
  viewForPathname,
} from "./routing";

describe("viewForPathname", () => {
  it("reserves settings and the rest of the console namespace", () => {
    expect(viewForPathname(SETTINGS_PATH)).toBe("settings");
    expect(viewForPathname(`${SETTINGS_PATH}/agents/enroll/10000000-0000-4000-8000-000000000001`)).toBe("notFound");
    expect(viewForPathname(`${CONSOLE_ROOT_PATH}/unknown`)).toBe("notFound");
    expect(viewForPathname(CONSOLE_ROOT_PATH)).toBe("embed");
  });

  it("returns paths outside the console namespace to haku-ui", () => {
    expect(viewForPathname(HOME_PATH)).toBe("embed");
    expect(viewForPathname("/garden/a.md")).toBe("embed");
  });
});

describe("rememberEmbedPath", () => {
  beforeEach(() => sessionStorage.clear());

  it("remembers haku-ui paths and ignores console paths", () => {
    rememberEmbedPath("/garden/a.md");
    expect(rememberedEmbedPath()).toBe("/garden/a.md");
    rememberEmbedPath(SETTINGS_PATH);
    expect(rememberedEmbedPath()).toBe("/garden/a.md");
  });
});
