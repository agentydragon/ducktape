import { camelizeObjectKeys, decamelizeObjectKeys } from "./lib/casing.js";
import { getJson, postJson } from "./lib/backend_client.js";
import { zBootstrapResponse, zScenarioSetInput, zScenarioSetRunResponse } from "./lib/api/schema.zod.mjs";

export async function fetchAugurBootstrap({ signal } = {}) {
  return camelizeObjectKeys(zBootstrapResponse.parse(await getJson("/api/bootstrap", signal)));
}

export async function runScenarioSet(scenarioSet, { signal } = {}) {
  const request = zScenarioSetInput.parse(decamelizeObjectKeys(scenarioSet));
  return camelizeObjectKeys(zScenarioSetRunResponse.parse(await postJson("/api/scenario_sets/run", request, signal)));
}
