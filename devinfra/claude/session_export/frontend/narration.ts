const NARRATION_SIGNATURE_OUTER_FIELD = 2n;
const NARRATION_SIGNATURE_PAYLOAD_FIELD = 1n;
const NARRATION_SIGNATURE_TYPE_FIELD = 8n;

type Varint = { value: bigint; next: number };

function readVarint(bytes: Uint8Array, offset: number): Varint | undefined {
  let value = 0n;
  for (let index = 0; index < 10; index += 1) {
    const position = offset + index;
    if (position >= bytes.length) return undefined;
    const byte = bytes[position]!;
    value |= BigInt(byte & 0x7f) << BigInt(index * 7);
    if ((byte & 0x80) === 0) return { value, next: position + 1 };
  }
  return undefined;
}

function visitFields(bytes: Uint8Array, onBytes: (field: bigint, value: Uint8Array) => void): boolean {
  let offset = 0;
  while (offset < bytes.length) {
    const tag = readVarint(bytes, offset);
    if (tag === undefined) return false;
    const wireType = Number(tag.value & 7n);
    const field = tag.value >> 3n;
    offset = tag.next;

    switch (wireType) {
      case 0: {
        const value = readVarint(bytes, offset);
        if (value === undefined) return false;
        offset = value.next;
        break;
      }
      case 1:
        if (offset + 8 > bytes.length) return false;
        offset += 8;
        break;
      case 2: {
        const length = readVarint(bytes, offset);
        if (length === undefined || length.value > BigInt(bytes.length - length.next)) return false;
        const end = length.next + Number(length.value);
        onBytes(field, bytes.subarray(length.next, end));
        offset = end;
        break;
      }
      case 5:
        if (offset + 4 > bytes.length) return false;
        offset += 4;
        break;
      default:
        return false;
    }
  }
  return true;
}

function readBytesField(bytes: Uint8Array, fieldNumber: bigint): Uint8Array | undefined {
  let value: Uint8Array | undefined;
  return visitFields(bytes, (field, fieldValue) => {
    if (field === fieldNumber) value = fieldValue;
  })
    ? value
    : undefined;
}

const utf8 = new TextDecoder("utf-8", { ignoreBOM: true });

function decodeUtf8Field(bytes: Uint8Array, fieldNumber: bigint): string | undefined {
  const value = readBytesField(bytes, fieldNumber);
  return value === undefined ? undefined : utf8.decode(value);
}

/** Read the type marker from Claude's nested base64 protobuf signature. */
export function decodeNarrationSignatureType(signature: unknown): string | undefined {
  if (typeof signature !== "string" || signature === "") return undefined;
  let binary: string;
  try {
    binary = atob(signature);
  } catch (error) {
    // An invalid optional signature is ordinary thinking; unrelated decoder errors should surface.
    if (typeof error === "object" && error !== null && "name" in error && error.name === "InvalidCharacterError") {
      return undefined;
    }
    throw error;
  }
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  const payload = readBytesField(bytes, NARRATION_SIGNATURE_OUTER_FIELD);
  if (payload === undefined) return undefined;
  const nested = readBytesField(payload, NARRATION_SIGNATURE_PAYLOAD_FIELD);
  return nested === undefined ? undefined : decodeUtf8Field(nested, NARRATION_SIGNATURE_TYPE_FIELD);
}

export function isNarrationSignature(signature: unknown): boolean {
  return decodeNarrationSignatureType(signature) === "narration";
}
