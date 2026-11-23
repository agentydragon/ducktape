import codegen from "shift-codegen";
import shiftFuzzer from "shift-fuzzer";
import { mulberry32 } from "../seeded_random.mjs";
import { composeModernExpressions } from "./modern_syntax.mjs";
import { parseModule, walk } from "./syntax_inventory.mjs";

function parseCandidate(source) {
  try {
    return parseModule(source);
  } catch {
    return null;
  }
}

// The hidden marker must be unobservable by the original program.
function canInstrument(ast, marker) {
  const forbidden = new Set(["globalThis", "global", "self", "window", "eval", "Function", "Reflect", "Proxy", marker]);
  let safe = true;
  walk(ast, (node) => {
    if (
      node.type === "ThisExpression" ||
      node.type === "ImportExpression" ||
      (node.type === "Identifier" && forbidden.has(node.name))
    )
      safe = false;
  });
  return safe;
}

// The emitter continues the grammar's random stream when it chooses a module for each statement.
export function shiftCandidate(seed, { script, modern }) {
  const random = mulberry32(seed);
  const shift = (script ? shiftFuzzer.fuzzScript : shiftFuzzer.fuzzModule)(
    new shiftFuzzer.FuzzerState({ maxDepth: 4, rng: random })
  );
  let source = codegen.default ? codegen.default(shift) : codegen(shift);
  let ast = parseCandidate(source);
  let sites = 0;
  let marker = null;
  if (ast && modern) {
    marker = `__shift_modern_marker_${seed}`;
    if (!canInstrument(ast, marker)) return { source, skip: "instrumentationRejected", sites };
    const composed = composeModernExpressions(source, ast, mulberry32(seed ^ 0x8b11c5), marker);
    source = composed.source;
    sites = composed.sites;
    ast = parseCandidate(source);
  }
  if (!ast) return { source, skip: "parseRejected", sites };
  let skip;
  if (ast.body.some((s) => /^(Import|Export)/.test(s.type))) skip = "moduleSyntax";
  else if (ast.body.length < 2 || ast.body.length > 30) skip = "small";
  return { source, ast, shift, random, marker, sites, skip };
}
