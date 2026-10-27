import { camelizeObjectKeys, decamelizeObjectKeys, type CamelCasedDeep } from "./lib/casing";
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
} from "./lib/api/schema.zod";

type FetchOptions = { signal?: AbortSignal };

function describeErrorBody(body) {
  if (!body) return "";
  if (typeof body === "string") return body.slice(0, 500);
  if (typeof body !== "object") return String(body).slice(0, 500);
  const detail = body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((entry) => JSON.stringify(entry)).join("; ");
  return JSON.stringify(body).slice(0, 500);
}

async function readJsonResponse(path, response) {
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

async function getJson(path, signal) {
  return readJsonResponse(path, await fetch(path, { method: "GET", signal }));
}

async function postJson(path, body, signal) {
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

async function postBlob(path, body, signal) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(decamelizeObjectKeys(body)),
    signal,
  });
  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    const errorBody = contentType.includes("application/json") ? await response.json() : await response.text();
    throw new Error(`Backend ${path} failed (${response.status}): ${describeErrorBody(errorBody)}`);
  }
  return response.blob();
}

// Anything with a Zod-like `.parse` (structural, so we don't import zod here). `T` is the wire
// (snake_case) output type; the helpers below carry it through camelization as `CamelCasedDeep<T>`.
type Parser<T> = { parse: (data: unknown) => T };
type SchemaInput<TSchema extends Parser<unknown>> = TSchema extends { _input: infer Input } ? Input : never;
type CamelCasedSchemaInput<TSchema extends Parser<unknown>> = CamelCasedDeep<SchemaInput<TSchema>>;

// GET a JSON resource, validate it against its wire schema, and camelize — the single place a
// read does this. Returns the camelCase-typed body.
async function apiGet<T>(path: string, schema: Parser<T>, signal?: AbortSignal) {
  return camelizeObjectKeys(schema.parse(await getJson(path, signal)));
}

// POST a camelCase `body`: decamelize + validate it against the request schema, then validate and
// camelize the response — the single place a write does this.
async function apiPost<TRequestSchema extends Parser<unknown>, TResponse>(
  path: string,
  requestSchema: TRequestSchema,
  responseSchema: Parser<TResponse>,
  body: CamelCasedSchemaInput<TRequestSchema>,
  signal?: AbortSignal
) {
  const request = requestSchema.parse(decamelizeObjectKeys(body));
  return camelizeObjectKeys(responseSchema.parse(await postJson(path, request, signal)));
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
  projectionSummaryRequest: CamelCasedSchemaInput<typeof zProductProjectionRequest>,
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

export type ProductProjectionSummaryRequest = CamelCasedSchemaInput<typeof zProductProjectionRequest>;

export function fetchProductRollout(
  rolloutRequest: CamelCasedSchemaInput<typeof zRolloutRequest>,
  { signal }: FetchOptions = {}
) {
  return apiPost("/api/product/projections/rollout", zRolloutRequest, zRolloutResponse, rolloutRequest, signal);
}

export function fetchCalibrationRun(
  calibrationRunRequest: CamelCasedSchemaInput<typeof zCalibrationRunRequest>,
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
  budgetSnapshotRequest: CamelCasedSchemaInput<typeof zBudgetSnapshotRequest>,
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

export type BudgetSnapshotRequest = CamelCasedSchemaInput<typeof zBudgetSnapshotRequest>;

export function fetchBudgetTransactions(
  budgetTransactionsRequest: CamelCasedSchemaInput<typeof zBudgetTransactionsRequest>,
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
  budgetSummaryCsvRequest: CamelCasedSchemaInput<typeof zBudgetSummaryCsvRequest>,
  { signal }: FetchOptions = {}
) {
  return postBlob("/api/budget/snapshot.csv", budgetSummaryCsvRequest, signal);
}

export function fetchBudgetTransactionsCsv(
  budgetTransactionsRequest: CamelCasedSchemaInput<typeof zBudgetTransactionsRequest>,
  { signal }: FetchOptions = {}
) {
  return postBlob("/api/budget/transactions.csv", budgetTransactionsRequest, signal);
}
