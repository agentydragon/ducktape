// @vitest-environment happy-dom

import { describe, expect, it, vi } from "vitest";

import { decodeNarrationSignatureType, isNarrationSignature } from "./narration";

function encodeVarint(value: number): number[] {
  const bytes: number[] = [];
  let remaining = value;
  while (remaining >= 0x80) {
    bytes.push((remaining & 0x7f) | 0x80);
    remaining = Math.floor(remaining / 128);
  }
  bytes.push(remaining);
  return bytes;
}

function concat(...parts: number[][]): number[] {
  return parts.flat();
}

function bytesField(fieldNumber: number, value: number[]): number[] {
  return concat(encodeVarint(fieldNumber * 8 + 2), encodeVarint(value.length), value);
}

function scalarField(fieldNumber: number, value: number): number[] {
  return concat(encodeVarint(fieldNumber * 8), encodeVarint(value));
}

function fixed64Field(fieldNumber: number, value: number[]): number[] {
  if (value.length !== 8) throw new Error("A fixed64 fixture must contain eight bytes");
  return concat(encodeVarint(fieldNumber * 8 + 1), value);
}

function fixed32Field(fieldNumber: number, value: number[]): number[] {
  if (value.length !== 4) throw new Error("A fixed32 fixture must contain four bytes");
  return concat(encodeVarint(fieldNumber * 8 + 5), value);
}

function signatureForType(
  type: string,
  extraInner: number[] = [],
  extraMiddle: number[] = [],
  extraOuter: number[] = []
): string {
  const encoder = new TextEncoder();
  const inner = concat(extraInner, bytesField(8, [...encoder.encode(type)]));
  const middle = concat(extraMiddle, bytesField(1, inner));
  const outer = concat(extraOuter, bytesField(2, middle));
  return btoa(String.fromCharCode(...outer));
}

describe("narration signature decoding", () => {
  it("follows the nested bytes fields and accepts only the narration marker", () => {
    const signature = signatureForType("narration");
    expect(decodeNarrationSignatureType(signature)).toBe("narration");
    expect(isNarrationSignature(signature)).toBe(true);
    expect(decodeNarrationSignatureType(signatureForType("thinking"))).toBe("thinking");
    expect(isNarrationSignature(signatureForType("thinking"))).toBe(false);
  });

  it("skips unknown varint, fixed64, bytes, and fixed32 fields at every nesting level", () => {
    const signature = signatureForType(
      "narration",
      concat(
        scalarField(20, 123),
        fixed64Field(21, Array(8).fill(0x21)),
        bytesField(22, [0x22]),
        fixed32Field(23, [1, 2, 3, 4])
      ),
      concat(
        scalarField(30, 123),
        fixed64Field(31, Array(8).fill(0x31)),
        bytesField(32, [0x32]),
        fixed32Field(33, [5, 6, 7, 8])
      ),
      concat(
        scalarField(40, 123),
        fixed64Field(41, Array(8).fill(0x41)),
        bytesField(42, [0x42]),
        fixed32Field(43, [9, 10, 11, 12])
      )
    );
    expect(isNarrationSignature(signature)).toBe(true);
  });

  it("uses the last matching bytes field at each nesting level", () => {
    const marker = [...new TextEncoder().encode("narration")];
    const inner = concat(bytesField(8, marker), bytesField(8, [...new TextEncoder().encode("other")]));
    const middle = concat(bytesField(1, inner), bytesField(1, []));
    const outer = concat(bytesField(2, middle), bytesField(2, []));
    const signature = btoa(String.fromCharCode(...outer));
    expect(decodeNarrationSignatureType(signature)).toBeUndefined();

    const finalInner = concat(bytesField(8, [...new TextEncoder().encode("other")]), bytesField(8, marker));
    const finalMiddle = concat(bytesField(1, []), bytesField(1, finalInner));
    const finalOuter = concat(bytesField(2, []), bytesField(2, finalMiddle));
    expect(isNarrationSignature(btoa(String.fromCharCode(...finalOuter)))).toBe(true);
  });

  it("returns no marker for missing, invalid, truncated, or unsupported protobuf data", () => {
    const malformed = [
      "%not-base64%",
      "gA==", // unfinished varint
      "Cw==", // unsupported group-start wire type
      "DA==", // unsupported group-end wire type
      "Dg==", // unsupported wire type 6
      "Dw==", // unsupported wire type 7
      "CQ==", // truncated fixed64
      "DQ==", // truncated fixed32
      "EgUA", // declared length exceeds available bytes
      btoa(String.fromCharCode(...bytesField(2, bytesField(3, [])))), // missing nested field 1
      btoa(String.fromCharCode(...bytesField(2, bytesField(1, bytesField(7, [1]))))), // missing field 8
    ];
    for (const signature of malformed) {
      expect(decodeNarrationSignatureType(signature), signature).toBeUndefined();
      expect(isNarrationSignature(signature), signature).toBe(false);
    }
    expect(decodeNarrationSignatureType(undefined)).toBeUndefined();
    expect(decodeNarrationSignatureType(42)).toBeUndefined();
    expect(decodeNarrationSignatureType("")).toBeUndefined();
  });

  it("ignores the browser's InvalidCharacterError but rethrows unrelated base64 decoder errors", () => {
    // happy-dom's atob throws a DOMException from a different realm than the test global.
    expect(decodeNarrationSignatureType("%not-base64%")).toBeUndefined();

    const unexpectedError = new Error("unexpected decoder failure");
    vi.stubGlobal("atob", () => {
      throw unexpectedError;
    });
    try {
      expect(() => decodeNarrationSignatureType("anything")).toThrow(unexpectedError);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("preserves the source decoder's BOM behavior", () => {
    const signature = signatureForType("\uFEFFnarration");
    expect(decodeNarrationSignatureType(signature)).toBe("\uFEFFnarration");
    expect(isNarrationSignature(signature)).toBe(false);
  });
});
