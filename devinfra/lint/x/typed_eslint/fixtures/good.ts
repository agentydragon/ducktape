import { loadValue } from "./api";
import { generatedPromise } from "./contract";

export async function loadBoth(): Promise<number> {
  return (await loadValue()) + (await generatedPromise());
}
