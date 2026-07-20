import { describe, expect, it } from "vitest";

import { formatArgv } from "./shell_lang.ts";

describe("formatArgv", () => {
  it("leaves safe unquoted tokens as-is", () => {
    expect(formatArgv(["rg", "-n", "TODO", "src/foo.py"])).toBe("rg -n TODO src/foo.py");
  });

  it("single-quotes an argument containing whitespace", () => {
    expect(formatArgv(["echo", "hello world"])).toBe("echo 'hello world'");
  });

  it("escapes an embedded single quote", () => {
    expect(formatArgv(["echo", "it's here"])).toBe("echo 'it'\\''s here'");
  });

  it("quotes an empty-string argument", () => {
    expect(formatArgv(["printf", "%s", ""])).toBe("printf %s ''");
  });

  it("quotes an argument containing shell metacharacters", () => {
    expect(formatArgv(["bash", "-lc", "cat a.txt | grep foo"])).toBe("bash -lc 'cat a.txt | grep foo'");
  });
});
