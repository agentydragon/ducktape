import { camelizeObjectKeys, decamelizeObjectKeys } from "./lib/casing.ts";
import {
  zBudgetSnapshotRequest,
  zBudgetSnapshotResponse,
  zBudgetTransactionsRequest,
  zBudgetTransactionsResponse,
  zCalibrationInfo,
  zCalibrationRunRequest,
  zCalibrationRunResponse,
  zCatalogResponse,
  zDeploymentInfo,
  zMetricFanRequest,
  zMetricFanResponse,
  zProductPortfolioResponse,
  zRolloutRequest,
  zRolloutResponse,
  zSettingsResponse,
} from "./lib/api/schema.zod.ts";

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

// Anything with a Zod-like `.parse` (structural, so we don't import zod here). `T` is the wire
// (snake_case) output type; the helpers below carry it through camelization as `CamelCasedDeep<T>`.
type Parser<T> = { parse: (data: unknown) => T };

// GET a JSON resource, validate it against its wire schema, and camelize — the single place a
// read does this. Returns the camelCase-typed body.
async function apiGet<T>(path: string, schema: Parser<T>, signal?: AbortSignal) {
  return camelizeObjectKeys(schema.parse(await getJson(path, signal)));
}

// POST a camelCase `body`: decamelize + validate it against the request schema, then validate and
// camelize the response — the single place a write does this.
async function apiPost<T>(
  path: string,
  requestSchema: Parser<unknown>,
  responseSchema: Parser<T>,
  body: unknown,
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

// `/api/calibration` returns the catalog info, or a null body when the deployment configures
// none — surface that as null rather than running it through the Zod object parser.
export async function fetchAugurCalibrationInfo({ signal }: FetchOptions = {}) {
  const body = await getJson("/api/calibration", signal);
  return body == null ? null : camelizeObjectKeys(zCalibrationInfo.parse(body));
}

export function fetchAugurDeployment({ signal }: FetchOptions = {}) {
  return apiGet("/api/deployment", zDeploymentInfo, signal);
}

export function fetchProductPortfolio({ signal }: FetchOptions = {}) {
  return apiGet("/api/product/portfolio", zProductPortfolioResponse, signal);
}

export function fetchProductMetricFan(metricFanRequest, { signal }: FetchOptions = {}) {
  return apiPost(
    "/api/product/projections/metric_fan",
    zMetricFanRequest,
    zMetricFanResponse,
    metricFanRequest,
    signal
  );
}

export function fetchProductRollout(rolloutRequest, { signal }: FetchOptions = {}) {
  return apiPost("/api/product/projections/rollout", zRolloutRequest, zRolloutResponse, rolloutRequest, signal);
}

export function fetchCalibrationRun(calibrationRunRequest, { signal }: FetchOptions = {}) {
  return apiPost(
    "/api/calibration/run",
    zCalibrationRunRequest,
    zCalibrationRunResponse,
    calibrationRunRequest,
    signal
  );
}

export function fetchBudgetSnapshot(budgetSnapshotRequest, { signal }: FetchOptions = {}) {
  return apiPost(
    "/api/budget/snapshot",
    zBudgetSnapshotRequest,
    zBudgetSnapshotResponse,
    budgetSnapshotRequest,
    signal
  );
}

export function fetchBudgetTransactions(budgetTransactionsRequest, { signal }: FetchOptions = {}) {
  return apiPost(
    "/api/budget/transactions",
    zBudgetTransactionsRequest,
    zBudgetTransactionsResponse,
    budgetTransactionsRequest,
    signal
  );
}
