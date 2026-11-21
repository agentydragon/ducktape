import type { JsonValue } from "@bufbuild/protobuf";

import { redirectToLogin } from "./operator_login";
import { parsedJson } from "./parsed_json";

/** `fetch`, except that a 401 sends the browser to the app login and never settles, so the page keeps
 * loading while the browser navigates away. */
export async function fetchWithLogin(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init);
  if (response.status === 401 && redirectToLogin()) return new Promise<never>(() => {});
  return response;
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
  if (!response.ok) throw await HttpError.fromResponse(response);
  return response.text();
}

/** `fetchText`, parsed as the JSON it holds. */
export async function fetchJson(input: RequestInfo | URL, init?: RequestInit): Promise<JsonValue> {
  return JSON.parse(await fetchText(input, init)) as JsonValue;
}

/** A non-2xx API response with its HTTP status, body, and gRPC code when available. */
export class HttpError extends Error {
  readonly status: number;
  readonly code: number | undefined;

  constructor(
    response: Response,
    readonly body: unknown
  ) {
    super(`HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}: ${displayableError(body)}`);
    this.status = response.status;
    // gRPC-Gateway streaming refusals use the same error envelope as an opened stream.
    const status = typeof body === "object" && body !== null && "error" in body ? body.error : body;
    this.code =
      typeof status === "object" && status !== null && "code" in status && typeof status.code === "number"
        ? status.code
        : undefined;
  }

  static async fromResponse(response: Response): Promise<HttpError> {
    const text = await response.text();
    const body = parsedJson(text);
    return new HttpError(response, body === undefined ? text : body);
  }
}

export function displayableError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null) return JSON.stringify(error);
  return String(error);
}
