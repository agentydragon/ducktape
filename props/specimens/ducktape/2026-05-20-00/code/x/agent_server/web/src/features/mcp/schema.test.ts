import { describe, it, expect } from "vitest";

import { TransportSpecZ, SseSpecZ, StdioSpecZ, InprocSpecZ, buildSpecFromForm } from "./schema";

describe("MCP schema (Zod)", () => {
  it("validates stdio spec", () => {
    const ok = StdioSpecZ.safeParse({
      transport: "stdio",
      command: "echo",
      args: ["hi"],
      env: { FOO: "1" },
    });
    expect(ok.success).toBe(true);
  });

  it("validates sse spec with _secs fields", () => {
    const ok = SseSpecZ.safeParse({
      transport: "sse",
      url: "http://x",
      headers: {},
      timeout_secs: 5,
      sse_read_timeout_secs: 300,
    });
    expect(ok.success).toBe(true);
  });

  it("rejects legacy sse fields", () => {
    const bad: any = {
      transport: "sse",
      url: "http://x",
      headers: {},
      timeout: 5,
      sse_read_timeout: 300,
    };
    const res = SseSpecZ.safeParse(bad);
    expect(res.success).toBe(false);
  });

  it("validates inproc spec", () => {
    const ok = InprocSpecZ.safeParse({
      transport: "inproc",
      factory: "pkg:factory",
      args: [],
      kwargs: {},
    });
    expect(ok.success).toBe(true);
  });

  it("discriminated union parses by transport", () => {
    const sse = TransportSpecZ.parse({
      transport: "sse",
      url: "u",
      headers: {},
      timeout_secs: 5,
      sse_read_timeout_secs: 300,
    });
    expect(sse.transport).toBe("sse");
    const stdio = TransportSpecZ.parse({ transport: "stdio", command: "x", args: [], env: {} });
    expect(stdio.transport).toBe("stdio");
  });
});

describe("buildSpecFromForm", () => {
  it("builds stdio from strings", () => {
    const { spec, errors } = buildSpecFromForm({
      transport: "stdio",
      stdioCommand: "cat",
      stdioArgs: '["-n"]',
      stdioEnv: '{"FOO":"1"}',
    });
    expect(errors).toEqual([]);
    expect(spec).toEqual({ transport: "stdio", command: "cat", args: ["-n"], env: { FOO: "1" } });
  });

  it("builds sse from numbers and strings", () => {
    const { spec, errors } = buildSpecFromForm({
      transport: "sse",
      sseUrl: "http://x",
      sseHeaders: "{}",
      sseTimeout: "7",
      sseReadTimeout: 123,
    });
    expect(errors).toEqual([]);
    expect(spec).toEqual({
      transport: "sse",
      url: "http://x",
      headers: {},
      timeout_secs: 7,
      sse_read_timeout_secs: 123,
    });
  });

  it("returns errors on invalid json", () => {
    const { errors } = buildSpecFromForm({
      transport: "stdio",
      stdioCommand: "",
      stdioArgs: "not json",
      stdioEnv: "{oops}",
    });
    expect(errors.length).toBeGreaterThan(0);
  });
});
