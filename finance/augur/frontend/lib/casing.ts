type CamelCase<S extends string> = S extends `${infer Head}_${infer Tail}`
  ? `${Head}${Capitalize<CamelCase<Tail>>}`
  : S;

// Deep snake_case -> camelCase key remapping, mirroring `camelizeObjectKeys` at the type level so
// the API client can carry the wire (Zod-inferred, snake_case) type through camelization with no
// `any`. Arrays recurse element-wise; `Record<string, V>` index signatures pass through unchanged
// (the general `string` key has no `_` boundary to rename), matching the runtime conversion.
export type CamelCasedDeep<T> = T extends readonly (infer U)[]
  ? CamelCasedDeep<U>[]
  : T extends object
    ? { [K in keyof T as CamelCase<K & string>]: CamelCasedDeep<T[K]> }
    : T;

function snakeToCamelKey(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_, char) => char.toUpperCase());
}

function camelToSnakeKey(key: string): string {
  return key.replace(/[A-Z]/g, (char) => `_${char.toLowerCase()}`);
}

function convertObjectKeys(value: unknown, keyFn: (key: string) => string): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => convertObjectKeys(item, keyFn));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [keyFn(key), convertObjectKeys(item, keyFn)])
  );
}

export function camelizeObjectKeys<T>(value: T): CamelCasedDeep<T> {
  return convertObjectKeys(value, snakeToCamelKey) as CamelCasedDeep<T>;
}

export function decamelizeObjectKeys(value: unknown): unknown {
  return convertObjectKeys(value, camelToSnakeKey);
}
