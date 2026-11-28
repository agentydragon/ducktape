import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { composeAcornOrigins, mapIntervals } from "./source_ownership.mjs";

export const locutilFile = "acorn/src/locutil.js";
const ordinals = [35, 36, 37, 38];
const names = ["Position", "SourceLocation", "getLineInfo"];

export function locutilPlacement(rows) {
  const entry = "entry.mjs";
  const owner = "locutil.mjs";
  return {
    entry,
    modules: {
      [owner]: ordinals.map((ordinal) => "#" + ordinal),
      [entry]: rows.filter(({ ordinal }) => !ordinals.includes(ordinal)).map(({ ordinal }) => "#" + ordinal),
    },
    order: [owner],
    sequential: true,
  };
}

export function locutilEvidence(parsed, units, rows, map, rollup) {
  const home = rows.filter(({ originals }) => originals.some(({ file }) => file === locutilFile));
  assert.deepEqual(
    home.map(({ ordinal }) => ordinal),
    ordinals
  );
  assert(home.every(({ classification }) => classification === "source_file"));

  const ordinal = ({ start, end }) => units.findIndex((unit) => unit.start <= start && end <= unit.end);
  const scope = parsed.scopes.acquire(parsed.ast, true);
  const bindings = Object.fromEntries(
    [...names, "nextLineBreak", "isNewLine"].map((name) => {
      const variable = scope.set.get(name);
      assert.equal(variable.defs.length, 1);
      const definition = ordinal(variable.defs[0].name);
      const reads = [
        ...new Set(variable.references.filter((ref) => ref.isRead()).map(({ identifier }) => ordinal(identifier))),
      ];
      assert(!reads.includes(-1), name);
      return [name, { definition, reads }];
    })
  );
  assert.deepEqual(
    names.map((name) => bindings[name].definition),
    [35, 37, 38]
  );
  assert.equal(bindings.nextLineBreak.definition, 23);
  assert(bindings.nextLineBreak.reads.includes(38));
  assert(bindings.isNewLine.reads.includes(23));
  assert(bindings.Position.reads.includes(36) && bindings.Position.reads.includes(38));
  const write = units[36].node.expression;
  assert.equal(write.operator, "=");
  assert.equal(write.left.object.object.name, "Position");
  assert.equal(write.left.object.property.name, "prototype");
  assert.equal(write.left.property.name, "offset");
  assert.deepEqual(
    parsed.scopes.globalScope.through.filter(({ identifier }) => ordinals.includes(ordinal(identifier))),
    []
  );
  const exports = units.at(-1).node;
  const exported = exports.specifiers
    .filter(({ local }) => names.includes(local.name))
    .map(({ local, exported }) => [local.name, exported.name]);
  assert.deepEqual(
    exported,
    names.map((name) => [name, name])
  );
  assert(!exports.specifiers.some(({ local }) => local.name === "nextLineBreak"));

  const compose = composeAcornOrigins(map.sourcesContent[0], rollup);
  const points = mapIntervals(parsed.code, map);
  const sourceRanges = home.map((row) => {
    const locations = points
      .filter((point) => point.source !== null && row.start <= point.start && point.start < row.end)
      .map(compose)
      .filter((origin) => origin?.source === locutilFile);
    const lines = locations.map(({ originalLine }) => originalLine + 1);
    return {
      ordinal: row.ordinal,
      utf16: [row.start, row.end],
      bytes: [row.startByte, row.endByte],
      mappedBytes: row.originals[0].mappedBytes,
      upstreamLines: [Math.min(...lines), Math.max(...lines)],
    };
  });
  assert.deepEqual(
    sourceRanges.map(({ upstreamLines }) => upstreamLines),
    [
      [6, 10],
      [12, 14],
      [17, 22],
      [31, 38],
    ]
  );
  return { file: locutilFile, sourceRanges, bindings, exported };
}

const point = ({ line, column }) => [line, column];
const load = (file) => import(pathToFileURL(file).href);

// Exercise the ordinary consumer path: parse real source with locations,
// ranges, comment and token collection, then map a diagnostic through getLineInfo.
export function observeLocations(api) {
  for (const name of names) assert.equal(api.Parser.acorn[name], api[name], name);
  assert(!Object.hasOwn(api, "nextLineBreak") && !Object.hasOwn(api.Parser.acorn, "nextLineBreak"));
  const program = '// note\r\nconst label = "𐐀";\u2028label;';
  function parse(sourceFile, directSourceFile) {
    const comments = [];
    const tokens = [];
    const tree = api.parse(program, {
      ecmaVersion: "latest",
      locations: true,
      ranges: true,
      sourceFile,
      directSourceFile,
      onToken: tokens,
      onComment: comments,
    });
    return { tree, comments, tokens };
  }
  const a = parse("alpha.js", "alpha.input");
  const b = parse("beta.js", "beta.input");
  const c = parse(null, null);
  const [decl, statement] = a.tree.body;
  const literal = decl.declarations[0].init;
  const second = b.tree.body[1];
  const bare = c.tree.body[1];
  const [comment] = a.comments;
  const identifier = a.tokens.find((token) => token.start === statement.start);
  for (const item of [a.tree, decl, literal, statement, second, bare, comment, identifier]) {
    assert.equal(Object.getPrototypeOf(item.loc), api.SourceLocation.prototype);
    assert.equal(Object.getPrototypeOf(item.loc.start), api.Position.prototype);
    assert.equal(Object.getPrototypeOf(item.loc.end), api.Position.prototype);
  }
  assert.equal(decl.loc.source, "alpha.js");
  assert.equal(decl.sourceFile, "alpha.input");
  assert.equal(second.loc.source, "beta.js");
  assert.equal(second.sourceFile, "beta.input");
  assert.equal(statement.loc.source, "alpha.js");
  assert(!Object.hasOwn(bare.loc, "source"));
  assert.notEqual(statement.loc, second.loc);
  assert.notEqual(statement.loc.start, second.loc.start);
  const direct = api.getLineInfo(program, statement.start);
  assert.equal(Object.getPrototypeOf(direct), api.Position.prototype);
  assert.deepEqual(point(direct), [3, 0]);
  assert.deepEqual(point(statement.loc.start), [3, 0]);
  const shifted = direct.offset(4);
  assert.equal(Object.getPrototypeOf(shifted), api.Position.prototype);
  assert.notEqual(shifted, direct);
  assert.deepEqual(point(shifted), [3, 4]);
  assert.deepEqual(point(direct), [3, 0]);

  const broken = program + "\r\nlet = ;";
  let error;
  assert.throws(
    () => api.parse(broken, { ecmaVersion: "latest", locations: true }),
    (caught) => {
      error = caught;
      return error instanceof SyntaxError;
    }
  );
  assert.equal(Object.getPrototypeOf(error.loc), api.Position.prototype);
  assert.deepEqual(point(error.loc), point(api.getLineInfo(broken, error.pos)));
  assert.equal(error.loc.line, 4);
  assert.equal(program.slice(...literal.range), '"𐐀"');
  assert.deepEqual(point(literal.loc.end), [2, 18]); // Columns count UTF-16 units.
  assert.deepEqual(point(comment.loc.end), [1, 7]);
  return {
    names: Object.keys(api),
    ctorNames: [api.Position.name, api.SourceLocation.name],
    nodes: [decl, literal, statement, second, bare, comment, identifier].map(({ loc, range, sourceFile }) => ({
      start: point(loc.start),
      end: point(loc.end),
      source: loc.source ?? null,
      range,
      sourceFile: sourceFile ?? null,
    })),
    error: { pos: error.pos, raisedAt: error.raisedAt, loc: point(error.loc) },
  };
}

export async function checkLocutilOutput(entry, owner, expected) {
  const api = await load(entry);
  const locations = await load(owner);
  const { names: actualNames, ...actual } = observeLocations(api);
  const { names: originalNames, ...original } = expected;
  assert.deepEqual(actual, original);
  for (const name of names) assert.equal(api[name], locations[name], name);
  return {
    locationsMatch: true,
    extraExports: actualNames.filter((name) => !originalNames.includes(name)),
    missingExports: originalNames.filter((name) => !actualNames.includes(name)),
  };
}

export async function locutilConsumer(source) {
  const api = await load(source);
  const expected = observeLocations(api);
  // Preserve names and use the actual parser, but give its locations the
  // wrong source state. A namespace-only or line-only check misses this.
  const wrong = { ...api, parse: (code, opts) => api.parse(code, { ...opts, sourceFile: "wrong.js" }) };
  assert.throws(() => observeLocations(wrong), /alpha\.js/);
  // Equal fields do not establish that the constructor cell survived.
  const wrongCell = {
    ...api,
    getLineInfo(input, offset) {
      const { line, column } = api.getLineInfo(input, offset);
      return { line, column };
    },
  };
  wrongCell.Parser = { acorn: { ...api.Parser.acorn, getLineInfo: wrongCell.getLineInfo } };
  assert.throws(() => observeLocations(wrongCell), { code: "ERR_ASSERTION" });
  return expected;
}
