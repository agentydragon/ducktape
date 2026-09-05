import type { ThreadDefaults } from "./client";

/** Only edited fields are stored on the Sandbox, so later preset defaults remain live. */
export function changedDefaults(current: ThreadDefaults, inherited: ThreadDefaults): ThreadDefaults | undefined {
  const changed = Object.fromEntries(
    Object.entries(current).filter(([field, value]) => value !== inherited[field as keyof ThreadDefaults])
  ) as ThreadDefaults;
  return Object.keys(changed).length > 0 ? changed : undefined;
}

/** Explicit Sandbox-level fields replace current preset defaults, including an empty instruction. */
export function effectiveThreadDefaults(inherited: ThreadDefaults, overrides: ThreadDefaults): ThreadDefaults {
  const explicit = Object.fromEntries(Object.entries(overrides).filter(([, value]) => value != null));
  return { ...inherited, ...explicit };
}
