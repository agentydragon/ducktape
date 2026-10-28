import { generatedValue } from "./contract";

export async function readValue(): Promise<number> {
  return await generatedValue();
}
