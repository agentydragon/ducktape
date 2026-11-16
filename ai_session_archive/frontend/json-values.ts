export type ObjectValue = Record<string, unknown>;

export function object(value: unknown): ObjectValue | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as ObjectValue) : null;
}

export function string(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function nonemptyString(value: unknown): string | null {
  const candidate = string(value);
  return candidate !== null && candidate.trim().length > 0 ? candidate : null;
}
