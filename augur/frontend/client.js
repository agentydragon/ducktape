import { camelizeObjectKeys, decamelizeObjectKeys } from "./lib/casing.js";
import {
  zBootstrapResponse,
  zCalibrationRunRequest,
  zCalibrationRunResponse,
  zDeploymentInfo,
  zMetricFanRequest,
  zMetricFanResponse,
  zProductPortfolioResponse,
  zRolloutRequest,
  zRolloutResponse,
} from "./lib/api/schema.zod.mjs";

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

export async function fetchAugurBootstrap({ signal } = {}) {
  return camelizeObjectKeys(zBootstrapResponse.parse(await getJson("/api/bootstrap", signal)));
}

export async function fetchAugurDeployment({ signal } = {}) {
  return camelizeObjectKeys(zDeploymentInfo.parse(await getJson("/api/deployment", signal)));
}

export async function fetchProductPortfolio({ signal } = {}) {
  return camelizeObjectKeys(zProductPortfolioResponse.parse(await getJson("/api/product/portfolio", signal)));
}

export async function fetchProductMetricFan(metricFanRequest, { signal } = {}) {
  const request = zMetricFanRequest.parse(decamelizeObjectKeys(metricFanRequest));
  return camelizeObjectKeys(
    zMetricFanResponse.parse(await postJson("/api/product/projections/metric_fan", request, signal))
  );
}

export async function fetchProductRollout(rolloutRequest, { signal } = {}) {
  const request = zRolloutRequest.parse(decamelizeObjectKeys(rolloutRequest));
  return camelizeObjectKeys(
    zRolloutResponse.parse(await postJson("/api/product/projections/rollout", request, signal))
  );
}

export async function fetchCalibrationRun(calibrationRunRequest, { signal } = {}) {
  const request = zCalibrationRunRequest.parse(decamelizeObjectKeys(calibrationRunRequest));
  return camelizeObjectKeys(zCalibrationRunResponse.parse(await postJson("/api/calibration/run", request, signal)));
}
