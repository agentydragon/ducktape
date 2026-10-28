import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { decodeSample, encodeSample } from "./consumer.js";
import type { SampleJson } from "./proto_es_generated/devinfra/x/schema_codegen/protobuf/sample_pb.js";

const RUNFILES_DIRECTORY = process.env.RUNFILES_DIR ?? process.env.TEST_SRCDIR;
const RUNFILES_WORKSPACE = process.env.TEST_WORKSPACE ?? "_main";
const PACKAGE_RUNFILES = `${RUNFILES_WORKSPACE}/devinfra/x/schema_codegen/protobuf`;

function parseFixture(fixture: string): SampleJson {
  if (RUNFILES_DIRECTORY === undefined) {
    throw new Error("Bazel did not provide a runfiles directory");
  }
  const output = readFileSync(join(RUNFILES_DIRECTORY, PACKAGE_RUNFILES, fixture), "utf8");
  return JSON.parse(output) as SampleJson;
}

describe("generated protobuf JSON boundary", () => {
  it.each([
    ["python_fixture.json", "python"],
    ["go_fixture.json", "go"],
  ])("decodes %s producer output", (filename, producer) => {
    const fixture = parseFixture(filename);
    const sample = decodeSample(fixture);

    expect(sample.sampleId).toBe("sample-17");
    expect(sample.sequence).toBe(9_007_199_254_740_993n);
    expect(sample.status).toBe(37);
    expect(sample.note).toBe("");
    expect(sample.payload.case).toBe("textPayload");
    expect(sample.metadata?.producer).toBe(producer);
    const encoded = encodeSample(sample);
    expect(encoded.sequence).toBe("9007199254740993");
    expect(encoded.status).toBe(37);
    expect(encoded.observedAt).toBe("2026-12-01T12:10:45.123Z");
  });

  it("distinguishes an absent optional string from a present empty string", () => {
    expect(decodeSample({}).note).toBeUndefined();
    expect(decodeSample({ note: "" }).note).toBe("");
  });

  it("rejects malformed ProtoJSON int64 values", () => {
    expect(() => decodeSample({ sequence: "not-an-int64" })).toThrow();
  });
});
