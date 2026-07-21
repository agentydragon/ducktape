import { beforeEach, describe, expect, it } from "vitest";

import {
  CONSOLE_ROOT_PATH,
  HOME_PATH,
  OAUTH_RESULT_PATH_PREFIX,
  oauthResultIdForPathname,
  rememberedEmbedPath,
  rememberEmbedPath,
  SETTINGS_PATH,
  TOOL_CALLS_PATH,
  viewForPathname,
} from "./routing.ts";

describe("viewForPathname", () => {
  it("reserves only the _console namespace for trusted pages", () => {
    expect(viewForPathname(SETTINGS_PATH)).toBe("settings");
    expect(viewForPathname(TOOL_CALLS_PATH)).toBe("toolCalls");
    expect(viewForPathname(`${OAUTH_RESULT_PATH_PREFIX}/8de5eb42-a3ce-4c83-9b13-59678c399ba3`)).toBe("oauthResult");
    expect(viewForPathname(`${CONSOLE_ROOT_PATH}/unknown`)).toBe("notFound");
    expect(viewForPathname(CONSOLE_ROOT_PATH)).toBe("embed");
  });

  it("accepts only canonical UUIDv4 OAuth result routes", () => {
    const id = "8de5eb42-a3ce-4c83-9b13-59678c399ba3";
    expect(oauthResultIdForPathname(`${OAUTH_RESULT_PATH_PREFIX}/${id}`)).toBe(id);
    expect(oauthResultIdForPathname(`${OAUTH_RESULT_PATH_PREFIX}/not-a-result`)).toBeNull();
    expect(viewForPathname(`${OAUTH_RESULT_PATH_PREFIX}/not-a-result`)).toBe("notFound");
  });

  it("returns every non-console path, including the old history path, to haku-ui", () => {
    expect(viewForPathname(HOME_PATH)).toBe("embed");
    expect(viewForPathname("/tool-calls")).toBe("embed");
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
