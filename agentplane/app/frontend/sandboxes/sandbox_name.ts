// Mirrors the backend's SandboxName (agentplane/app/sandbox_models.py): a DNS label of at most 57 characters.
const SANDBOX_NAME_PATTERN = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const SANDBOX_NAME_MAX_LENGTH = 57;

/** Why `name` would be refused, or null when it is acceptable. */
export function sandboxNameError(name: string): string | null {
  if (name.length > SANDBOX_NAME_MAX_LENGTH) return `At most ${SANDBOX_NAME_MAX_LENGTH} characters`;
  if (!SANDBOX_NAME_PATTERN.test(name))
    return "Lowercase letters, digits and hyphens only; start and end with a letter or digit";
  return null;
}

/** `base` plus the lowest numeric suffix no existing Sandbox uses, shortened to stay a valid name. */
export function placeholderSandboxName(base: string, taken: ReadonlySet<string>): string {
  for (let suffix = 1; ; suffix++) {
    const tail = `-${suffix}`;
    const candidate = `${base.slice(0, SANDBOX_NAME_MAX_LENGTH - tail.length).replace(/-+$/, "")}${tail}`;
    if (!taken.has(candidate)) return candidate;
  }
}
