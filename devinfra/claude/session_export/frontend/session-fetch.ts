import { ApiError } from "./api";

export function retryable(reason: unknown): boolean {
  if (reason instanceof ApiError) return reason.status === 408 || reason.status === 429 || reason.status >= 500;
  return reason instanceof TypeError;
}
export function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException("Request aborted", "AbortError"));
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, delay);
    const abort = (): void => {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Request aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}
export async function retryTransient<T>(request: () => Promise<T>, signal: AbortSignal): Promise<T> {
  let delay = 500;
  while (!signal.aborted) {
    try {
      return await request();
    } catch (reason) {
      if (!retryable(reason)) throw reason;
      await waitForRetry(delay, signal);
      delay = Math.min(delay * 2, 30_000);
    }
  }
  throw new DOMException("Request aborted", "AbortError");
}
export function errorMessage(reason: unknown): string {
  return reason instanceof ApiError || reason instanceof Error ? reason.message : "Could not load sessions.";
}
