import {
  zBudgetSnapshotRequest,
  zBudgetSnapshotResponse,
  type zBudgetSummaryCsvRequest,
  zBudgetTransactionsRequest,
  zBudgetTransactionsResponse,
  zCalibrationInfo,
  zCalibrationRunRequest,
  zCalibrationRunResponse,
  zCatalogResponse,
  zDeploymentInfo,
  zProductPortfolioResponse,
  zProductProjectionRequest,
  zProductProjectionResponse,
  zRolloutRequest,
  zRolloutResponse,
  zSettingsResponse,
} from "./lib/schema_zod";
import type { z } from "zod";

type FetchOptions = { signal?: AbortSignal };

function describeErrorBody(body: unknown) {
  if (!body) return "";
  if (typeof body === "string") return body.slice(0, 500);
  if (typeof body !== "object") return String(body).slice(0, 500);
  const detail = "detail" in body ? body.detail : undefined;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((entry: unknown) => JSON.stringify(entry)).join("; ");
  return JSON.stringify(body).slice(0, 500);
}

async function readJsonResponse(path: string, response: Response) {
  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const body = isJson ? await response.json() : await response.text();
  if (!response.ok) {
    throw new Error(`Backend ${path} failed (${response.status}): ${describeErrorBody(body)}`);
  }
  if (!isJson) {
    throw new Error(`Backend ${path} response was not JSON: ${String(body).slice(0, 500)}`);
  }
  return body;
}

async function getJson(path: string, signal?: AbortSignal) {
  return readJsonResponse(path, await fetch(path, { method: "GET", signal }));
}

async function postJson(path: string, body: unknown, signal?: AbortSignal) {
  return readJsonResponse(
    path,
    await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal,
    })
  );
}

async function postBlob(path: string, body: unknown, signal?: AbortSignal) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    const errorBody = contentType.includes("application/json") ? await response.json() : await response.text();
    throw new Error(`Backend ${path} failed (${response.status}): ${describeErrorBody(errorBody)}`);
  }
  return response.blob();
}

async function apiGet<T>(path: string, schema: z.ZodType<T>, signal?: AbortSignal): Promise<T> {
  return schema.parse(await getJson(path, signal));
}

// Validate a wire-shaped request and response at the API boundary.
async function apiPost<TRequestSchema extends z.ZodTypeAny, TResponse>(
  path: string,
  requestSchema: TRequestSchema,
  responseSchema: z.ZodType<TResponse>,
  body: z.input<TRequestSchema>,
  signal?: AbortSignal
): Promise<TResponse> {
  const request = requestSchema.parse(body);
  return responseSchema.parse(await postJson(path, request, signal));
}

export function fetchAugurCatalog({ signal }: FetchOptions = {}) {
  return apiGet("/api/catalog", zCatalogResponse, signal);
}

export function fetchAugurSettings({ signal }: FetchOptions = {}) {
  return apiGet("/api/settings", zSettingsResponse, signal);
}

// `/api/calibration` returns the deployment's calibration catalog metadata (`CalibrationInfo`:
// label/issuer). Every deployment configures a catalog, so this is always present.
export function fetchAugurCalibrationInfo({ signal }: FetchOptions = {}) {
  return apiGet("/api/calibration", zCalibrationInfo, signal);
}

export function fetchAugurDeployment({ signal }: FetchOptions = {}) {
  return apiGet("/api/deployment", zDeploymentInfo, signal);
}

export function fetchProductPortfolio({ signal }: FetchOptions = {}) {
  return apiGet("/api/product/portfolio", zProductPortfolioResponse, signal);
}

export function fetchProductProjectionSummary(
  projectionSummaryRequest: z.input<typeof zProductProjectionRequest>,
  { signal }: FetchOptions = {}
) {
  return apiPost(
    "/api/product/projections/summary",
    zProductProjectionRequest,
    zProductProjectionResponse,
    projectionSummaryRequest,
    signal
  );
}

export function fetchProductRollout(rolloutRequest: z.input<typeof zRolloutRequest>, { signal }: FetchOptions = {}) {
  return apiPost("/api/product/projections/rollout", zRolloutRequest, zRolloutResponse, rolloutRequest, signal);
}

export function fetchCalibrationRun(
  calibrationRunRequest: z.input<typeof zCalibrationRunRequest>,
  { signal }: FetchOptions = {}
) {
  return apiPost(
    "/api/calibration/run",
    zCalibrationRunRequest,
    zCalibrationRunResponse,
    calibrationRunRequest,
    signal
  );
}

export function fetchBudgetSnapshot(
  budgetSnapshotRequest: z.input<typeof zBudgetSnapshotRequest>,
  { signal }: FetchOptions = {}
) {
  return apiPost(
    "/api/budget/snapshot",
    zBudgetSnapshotRequest,
    zBudgetSnapshotResponse,
    budgetSnapshotRequest,
    signal
  );
}

export function fetchBudgetTransactions(
  budgetTransactionsRequest: z.input<typeof zBudgetTransactionsRequest>,
  { signal }: FetchOptions = {}
) {
  return apiPost(
    "/api/budget/transactions",
    zBudgetTransactionsRequest,
    zBudgetTransactionsResponse,
    budgetTransactionsRequest,
    signal
  );
}

export function fetchBudgetSummaryCsv(
  budgetSummaryCsvRequest: z.input<typeof zBudgetSummaryCsvRequest>,
  { signal }: FetchOptions = {}
) {
  return postBlob("/api/budget/snapshot.csv", budgetSummaryCsvRequest, signal);
}

export function fetchBudgetTransactionsCsv(
  budgetTransactionsRequest: z.input<typeof zBudgetTransactionsRequest>,
  { signal }: FetchOptions = {}
) {
  return postBlob("/api/budget/transactions.csv", budgetTransactionsRequest, signal);
}
