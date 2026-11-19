export function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "message" in error && error.message) return String(error.message);
  return String(error);
}

export function isAbortError(error: unknown): boolean {
  return error != null && typeof error === "object" && "name" in error && error.name === "AbortError";
}
