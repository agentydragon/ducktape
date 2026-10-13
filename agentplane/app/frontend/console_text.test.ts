import { describe, expect, it } from "vitest";

import { consoleText } from "./console_text";

describe("the console text", () => {
  it("drops terminal control sequences and carriage returns", () => {
    expect(consoleText("", "\x1b[0;32m  OK  \x1b[0m Started\r\n\x1b]3008;start=1\x1b\\login: ")).toBe(
      "  OK   Started\nlogin: "
    );
  });

  it("keeps only the latest output once it outgrows its cap", () => {
    const text = consoleText("a".repeat(200_000), "tail");
    expect(text).toHaveLength(200_000);
    expect(text.endsWith("tail")).toBe(true);
  });
});
