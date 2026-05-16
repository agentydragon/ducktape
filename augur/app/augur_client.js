import { camelizeObjectKeys, decamelizeObjectKeys } from "./lib/casing.js";
import { getJson, postJson } from "./lib/backend_client.js";
import { schemas } from "./lib/api/schema.zod.mjs";

export async function fetchAugurBootstrap({ signal } = {}) {
  return camelizeObjectKeys(schemas.BootstrapResponse.parse(await getJson("/api/bootstrap", signal)));
}

export async function runScenarioSet(scenarioSet, { signal } = {}) {
  const request = schemas.ScenarioSet_Input.parse(decamelizeObjectKeys(scenarioSet));
  return camelizeObjectKeys(
    schemas.ScenarioSetRunResponse.parse(await postJson("/api/scenario_sets/run", request, signal))
  );
}
