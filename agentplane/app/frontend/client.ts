import createClient from "openapi-fetch";

import type { paths } from "./schema";
import { redirectToLogin } from "./operator_login";

export const api: ReturnType<typeof createClient<paths>> = createClient<paths>({ baseUrl: "" });

// 401 → app login; the never-settling promise keeps the page loading while the browser navigates away.
function unlessLoggedOut(response: Response): Response | Promise<never> {
  if (response.status === 401 && redirectToLogin()) return new Promise<never>(() => {});
  return response;
}

api.use({ onResponse: ({ response }) => unlessLoggedOut(response) });

/** `fetch` under `api`'s 401 policy, for the app's own routes that do not go through `api`. */
export async function fetchWithLogin(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return unlessLoggedOut(await fetch(input, init));
}

/** A non-2xx answer from the API: its status, and its body as openapi-fetch decoded it. */
export class HttpError extends Error {
  readonly status: number;

  constructor(
    response: Response,
    readonly body: unknown
  ) {
    super(`HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}: ${displayableError(body)}`);
    this.status = response.status;
  }
}

/** The data of an `api` answer, or its error thrown as an `HttpError`. */
export function ok<T>(answer: { data?: T; error?: unknown; response: Response }): T {
  if (answer.error !== undefined) throw new HttpError(answer.response, answer.error);
  return answer.data as T;
}
export function displayableError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null) return JSON.stringify(error);
  return String(error);
}
