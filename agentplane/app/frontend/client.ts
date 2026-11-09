import type { JsonValue } from "@bufbuild/protobuf";
import createClient from "openapi-fetch";

import { parsedJson } from "./parsed_json";

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

export type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** The body of a 2xx answer; any other is thrown as an `HttpError` holding its JSON body, or its
 * text where the body is not JSON. */
export async function fetchText(
  input: RequestInfo | URL,
  init?: RequestInit,
  fetcher: Fetcher = fetchWithLogin
): Promise<string> {
  const response = await fetcher(input, init);
  const text = await response.text();
  if (!response.ok) {
    const body = parsedJson(text);
    throw new HttpError(response, body === undefined ? text : body);
  }
  return text;
}

/** `fetchText`, parsed as the JSON it holds. */
export async function fetchJson(input: RequestInfo | URL, init?: RequestInit): Promise<JsonValue> {
  return JSON.parse(await fetchText(input, init)) as JsonValue;
}

/** A non-2xx answer from the API: its status, and its body as openapi-fetch decoded it. */
export class HttpError extends Error {
  readonly status: number;
  readonly code: number | undefined;

  constructor(
    response: Response,
    readonly body: unknown
  ) {
    super(`HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}: ${displayableError(body)}`);
    this.status = response.status;
    this.code =
      typeof body === "object" && body !== null && "code" in body && typeof body.code === "number"
        ? body.code
        : undefined;
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
