// Serialize the actual frontend's annotated units as Lean constructors. This is
// fixture generation, not a proof that Acorn or scope resolution is correct.
import { frontBundle } from "./frontend.mjs";

function lean(value) {
  if (value === null) return "Lean.Json.null";
  if (typeof value === "string") return `Lean.Json.str ${JSON.stringify(value)}`;
  if (typeof value === "boolean") return `Lean.Json.bool ${value}`;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error("unsupported fixture number");
    return `Lean.toJson (${value} : Nat)`;
  }
  if (Array.isArray(value)) return `Lean.Json.arr #[${value.map(lean).join(", ")}]`;
  return `Lean.Json.mkObj [${Object.entries(value)
    .map(([k, v]) => `(${JSON.stringify(k)}, ${lean(v)})`)
    .join(", ")}]`;
}

const units = frontBundle(process.argv[2]).units;
process.stdout.write(
  `import Lean.Data.Json\nnamespace IdentityFixture.Annotated\ndef units : Array Lean.Json := #[${units.map(lean).join(", ")}]\nend IdentityFixture.Annotated\n`
);
