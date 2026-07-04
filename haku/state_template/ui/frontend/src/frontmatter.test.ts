import { describe, expect, it } from "vitest";

import { parseFrontmatter } from "./frontmatter.ts";

describe("parseFrontmatter", () => {
  it("parses the YAML frontmatter block and returns the body separately", () => {
    const { data, body } = parseFrontmatter("---\nkind: improvement\ntitle: Alpha\n---\nbody text\n");
    expect(data).toEqual({ kind: "improvement", title: "Alpha" });
    expect(body).toBe("body text\n");
  });

  it("handles real YAML: quoted values with colons, comments", () => {
    const { data } = parseFrontmatter('---\n# a note\ntitle: "Tana: the pipe"\nstatus: open\n---\nx');
    expect(data.title).toBe("Tana: the pipe");
    expect(data.status).toBe("open");
    expect("# a note" in data).toBe(false);
  });

  it("degrades to empty data on malformed YAML instead of throwing", () => {
    const { data, body } = parseFrontmatter("---\n: : bad\n  indent\n---\nbody");
    expect(data).toEqual({});
    expect(body).toBe("body");
  });

  it("returns the whole text as body when there is no frontmatter", () => {
    expect(parseFrontmatter("just markdown")).toEqual({ data: {}, body: "just markdown" });
  });
});
