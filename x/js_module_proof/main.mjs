import { generate, validate } from "./checker.mjs";
import { generateEffects, validateEffects } from "./effects.mjs";
import { generateCombined, validateCombined } from "./combined.mjs";
import { generateBindings, validateBindings } from "./bindings.mjs";
import { readFile } from "node:fs/promises";

const [command, ...args] = process.argv.slice(2);

try {
  let result;
  if (command === "generate" && args.length === 3) {
    const spec = JSON.parse(await readFile(args[1], "utf8"));
    await (
      spec.kind === "effects"
        ? generateEffects
        : spec.kind === "combined"
          ? generateCombined
          : spec.kind === "bindings"
            ? generateBindings
            : generate
    )(...args);
    result = { result: "generated", directory: args[2] };
  } else if (command === "validate" && args.length === 3) {
    const spec = JSON.parse(await readFile(args[1], "utf8"));
    result = await (
      spec.kind === "effects"
        ? validateEffects
        : spec.kind === "combined"
          ? validateCombined
          : spec.kind === "bindings"
            ? validateBindings
            : validate
    )(...args);
  } else {
    throw new Error("usage: generate|validate ORIGINAL ASSIGNMENT OUTPUT_DIR");
  }
  console.log(JSON.stringify(result, null, 2));
  if (result.result === "unknown") process.exitCode = 2;
} catch (error) {
  console.error(JSON.stringify({ result: "unknown", reason: String(error) }));
  process.exitCode = 2;
}
