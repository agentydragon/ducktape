// Which standard and host features does a set of JavaScript files use? A static count, for the list of what a debundler
// has to support:
//
//   - free identifiers: a name no declaration in the file binds is a lookup on the global object;
//   - the static member paths that start at one (`Object.defineProperty`, `Array.prototype.slice`), followed through
//     `window`, `self`, `globalThis` and `global`, and through `const { keys } = Object`;
//   - the names of members of standard objects that occur as a static property of anything else (`x.map(f)`): the
//     receiver is unknown, so this is a bound by name, not a use;
//   - the module specifiers of host modules (`node:fs`, `bun:ffi`) and the keys of `import.meta`.
//
//   node host_inventory.mjs scan PATH... > inventory.json      (a file, a directory of .js/.mjs/.cjs files, or @LIST:
//                                                               a file with one path of a source file per line)
//   node host_inventory.mjs render [--full] inventory.json > report.md
//
// `render` prints only names that are public already: a global the `globals` package lists for the language, the web
// platform, Node.js, Bun or another host, a member path that resolves on this Node's own objects, a host module and a
// key of `import.meta` (the host defines that object). Every other name (a global the program or its page defines, a
// vendor-prefixed or retired web name, a member of an object Node does not have) is counted and not printed; `--full`
// prints those too. A file's name appears only in a parse error.
//
// What the count does not see: a global reached through a local alias (`var g = window; g.fetch`) or a computed key, a
// member reached through a value (`el.appendChild`, beyond the bound by name), and code built at run time.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { builtinModules, createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import globals from "globals";

const GLOBALS_VERSION = createRequire(import.meta.url)("globals/package.json").version;

/** The lists of the `globals` package that make up each environment. A name can be in several. */
export const ENVIRONMENTS = {
  ecmascript: ["builtin"],
  web: ["browser", "worker", "serviceworker", "sharedWorker", "audioWorklet", "paintWorklet"],
  node: ["node", "nodeBuiltin", "commonjs"],
  bun: ["bunBuiltin"],
  otherHosts: ["denoBuiltin", "webextensions", "wsh", "amd"],
};

/** Names that are the global object itself: `window.fetch` is the global `fetch`. */
const GLOBAL_OBJECTS = new Set(["globalThis", "window", "self", "global"]);

/** A member path longer than this is cut: `a.b.c.d` counts `a.b.c`. */
const MAX_MEMBERS = 3;

/** `process.env.X` names configuration of one program, not a feature of the host: the path stops at `process.env`. */
const OPAQUE_PATHS = new Set(["process.env"]);

const SOURCE_EXTENSIONS = new Set([".js", ".mjs", ".cjs"]);

export function environmentsOf(name) {
  return Object.entries(ENVIRONMENTS)
    .filter(([, lists]) => lists.some((list) => Object.hasOwn(globals[list], name)))
    .map(([environment]) => environment);
}

// ---- the standard objects of this Node, to check a member path against and to name the owners of a member

function lookup(object, key) {
  for (let o = object; o !== null && o !== undefined; o = Object.getPrototypeOf(o)) {
    const descriptor = Object.getOwnPropertyDescriptor(o, key);
    if (descriptor) return descriptor;
  }
  return undefined;
}

function globalValue(name) {
  const descriptor = lookup(globalThis, name);
  if (!descriptor) return undefined;
  if ("value" in descriptor) return descriptor.value;
  // Node defines some globals (`crypto`, `navigator`) as getters that load the module on first use
  try {
    return descriptor.get?.call(globalThis);
  } catch {
    return undefined;
  }
}

function isObject(value) {
  return (typeof value === "object" && value !== null) || typeof value === "function";
}

/** How many leading members of `members` exist on this Node's `root`. A getter ends the walk: it is not run. */
export function resolvedMembers(root, members) {
  let object = globalValue(root);
  let count = 0;
  for (const member of members) {
    if (!isObject(object)) break;
    const descriptor = lookup(object, member);
    if (!descriptor) break;
    count += 1;
    object = "value" in descriptor ? descriptor.value : undefined;
  }
  return count;
}

/** member name -> the standard objects that have it as an own string-keyed property (`map` -> `Array.prototype`, ...). */
export function standardMemberOwners() {
  const owners = new Map();
  const add = (label, object) => {
    if (!isObject(object)) return;
    for (const key of Object.getOwnPropertyNames(object)) {
      if (!owners.has(key)) owners.set(key, []);
      owners.get(key).push(label);
    }
  };
  for (const name of Object.keys(globals.builtin).sort()) {
    const value = globalValue(name);
    if (!isObject(value) || value === globalThis) continue;
    add(name, value);
    if (typeof value === "function" && isObject(value.prototype)) add(`${name}.prototype`, value.prototype);
  }
  // the standard objects no global names
  const typedArray = Object.getPrototypeOf(Int8Array);
  add("%TypedArray%", typedArray);
  add("%TypedArray%.prototype", typedArray.prototype);
  add("%GeneratorPrototype%", Object.getPrototypeOf(function* () {}).prototype);
  add("%AsyncGeneratorPrototype%", Object.getPrototypeOf(async function* () {}).prototype);
  add("%IteratorPrototype%", Object.getPrototypeOf(Object.getPrototypeOf([][Symbol.iterator]())));
  return owners;
}

// ---- one file

function parse(source) {
  // eslint-scope reads `range`
  const options = { ecmaVersion: "latest", allowHashBang: true, ranges: true };
  try {
    return { ast: acorn.parse(source, { ...options, sourceType: "module" }), sourceType: "module" };
  } catch {
    // a classic script or a CommonJS module: sloppy mode, and `return` at the top level
    const script = { ...options, sourceType: "script", allowReturnOutsideFunction: true };
    return { ast: acorn.parse(source, script), sourceType: "script" };
  }
}

const IDENTIFIER_NAME = /^[\p{ID_Start}$_][\p{ID_Continue}$\u200c\u200d]*$/u;

/** The name a member expression reads, when the source spells it: `a.b`, or `a["b"]` with a string that could be written `a.b`. */
function staticKey(member) {
  if (!member.computed && member.property.type === "Identifier") return member.property.name;
  const { property } = member;
  if (
    member.computed &&
    property.type === "Literal" &&
    typeof property.value === "string" &&
    IDENTIFIER_NAME.test(property.value)
  ) {
    return property.value;
  }
  return undefined;
}

/** Sets `parent` on every node and returns the nodes of the types asked for. Run after eslint-scope, which must not see `parent`. */
function linkParents(ast, wanted) {
  const found = [];
  const stack = [ast];
  while (stack.length > 0) {
    const node = stack.pop();
    if (wanted.has(node.type)) found.push(node);
    for (const key of Object.keys(node)) {
      if (key === "parent") continue;
      const value = node[key];
      if (Array.isArray(value)) {
        for (const child of value) {
          if (child && typeof child.type === "string") {
            child.parent = node;
            stack.push(child);
          }
        }
      } else if (value && typeof value.type === "string") {
        value.parent = node;
        stack.push(value);
      }
    }
  }
  return found;
}

/** How an expression is used by its parent: `call`, `new`, `write`, `typeof` or `read`. */
function usageOf(node) {
  const parent = node.parent;
  switch (parent?.type) {
    case "CallExpression":
      return parent.callee === node ? "call" : "read";
    case "TaggedTemplateExpression":
      return parent.tag === node ? "call" : "read";
    case "NewExpression":
      return parent.callee === node ? "new" : "read";
    case "AssignmentExpression":
      return parent.left === node ? "write" : "read";
    case "UpdateExpression":
      return "write";
    case "UnaryExpression":
      return parent.operator === "delete" ? "write" : parent.operator === "typeof" ? "typeof" : "read";
    default:
      return "read";
  }
}

/** The keys an object pattern takes out of `node`, when `node` is what the pattern destructures. */
function destructuredKeys(node) {
  const parent = node.parent;
  const pattern =
    parent?.type === "VariableDeclarator" && parent.init === node
      ? parent.id
      : parent?.type === "AssignmentExpression" && parent.right === node && parent.operator === "="
        ? parent.left
        : undefined;
  if (pattern?.type !== "ObjectPattern") return [];
  return pattern.properties.flatMap((property) => {
    if (property.type !== "Property") return [];
    if (!property.computed && property.key.type === "Identifier") return [property.key.name];
    if (
      property.key.type === "Literal" &&
      typeof property.key.value === "string" &&
      IDENTIFIER_NAME.test(property.key.value)
    ) {
      return [property.key.value];
    }
    return [];
  });
}

function hostModule(specifier) {
  if (specifier.startsWith("node:")) return { host: "node", name: specifier };
  if (specifier === "bun" || specifier.startsWith("bun:")) return { host: "bun", name: specifier };
  if (builtinModules.includes(specifier)) return { host: "node", name: `node:${specifier}` };
  return undefined;
}

class Inventory {
  constructor() {
    this.files = { module: 0, script: 0 };
    this.bytes = 0;
    this.globals = new Map(); // name -> { files, usage: {read, call, new, write, typeof}, paths: Map }
    this.memberNames = new Map(); // a standard member's name on some other receiver -> { files, call, read, write }
    this.hostModules = new Map(); // "node:fs" -> files
    this.otherSpecifiers = new Set();
    this.importMeta = new Map(); // key -> files
    this.dynamicImports = 0;
    this.owners = standardMemberOwners();
  }

  #count(map, key, file) {
    if (!map.has(key)) map.set(key, new Set());
    map.get(key).add(file);
  }

  #global(name) {
    if (!this.globals.has(name)) {
      this.globals.set(name, {
        files: new Set(),
        usage: { read: 0, call: 0, new: 0, write: 0, typeof: 0 },
        paths: new Map(),
      });
    }
    return this.globals.get(name);
  }

  #use(name, members, usage, file) {
    const entry = this.#global(name);
    entry.files.add(file);
    if (members.length === 0) {
      entry.usage[usage] += 1;
      return;
    }
    entry.usage.read += 1;
    for (let length = 1; length <= members.length; length += 1) {
      const key = members.slice(0, length).join(".");
      if (!entry.paths.has(key)) entry.paths.set(key, { read: 0, call: 0, new: 0, write: 0, typeof: 0 });
      entry.paths.get(key)[length === members.length ? usage : "read"] += 1;
    }
  }

  /** A reference to a name the file does not bind: follow the member path that starts at it. */
  #reference(reference, file, consumed) {
    let node = reference.identifier;
    let root = node.name;
    let members = [];
    for (;;) {
      const parent = node.parent;
      if (parent?.type === "ChainExpression") {
        node = parent;
        continue;
      }
      if (parent?.type !== "MemberExpression" || parent.object !== node) break;
      const key = staticKey(parent);
      if (key === undefined) break;
      consumed.add(parent);
      node = parent;
      if (GLOBAL_OBJECTS.has(root) && members.length === 0) {
        this.#use(root, [], "read", file);
        root = key;
        continue;
      }
      members.push(key);
      if (members.length === MAX_MEMBERS || OPAQUE_PATHS.has([root, ...members].join("."))) break;
    }
    let usage = usageOf(node);
    if (members.length === 0 && node === reference.identifier && reference.isWrite() && !reference.isRead()) {
      usage = "write"; // also a target inside a destructuring pattern or a `for-in` head
    }
    // `f.call(x)` and `f.apply(x, args)` call `f`; `f.bind(x)` reads it
    if (usage === "call" && members.length > 0 && ["call", "apply", "bind"].includes(members.at(-1))) {
      usage = members.at(-1) === "bind" ? "read" : "call";
      members = members.slice(0, -1);
    }
    this.#use(root, members, usage, file);
    if (GLOBAL_OBJECTS.has(root) && members.length === 0) {
      for (const key of destructuredKeys(node)) this.#use(key, [], "read", file);
    } else if (members.length < MAX_MEMBERS && !OPAQUE_PATHS.has([root, ...members].join("."))) {
      for (const key of destructuredKeys(node)) this.#use(root, [...members, key], "read", file);
    }
  }

  addFile(file, source) {
    const { ast, sourceType } = parse(source);
    this.files[sourceType] += 1;
    this.bytes += Buffer.byteLength(source);
    const scopes = eslintScope.analyze(ast, { ecmaVersion: 2024, sourceType, ignoreEval: true });
    const nodes = linkParents(
      ast,
      new Set([
        "MemberExpression",
        "MetaProperty",
        "ImportDeclaration",
        "ExportNamedDeclaration",
        "ExportAllDeclaration",
        "ImportExpression",
        "CallExpression",
      ])
    );
    const consumed = new Set();
    for (const reference of scopes.globalScope.through) this.#reference(reference, file, consumed);

    for (const node of nodes) {
      switch (node.type) {
        case "MemberExpression": {
          const key = staticKey(node);
          if (consumed.has(node) || key === undefined || !this.owners.has(key)) break;
          if (!this.memberNames.has(key)) this.memberNames.set(key, { files: new Set(), call: 0, read: 0, write: 0 });
          const entry = this.memberNames.get(key);
          entry.files.add(file);
          const usage = usageOf(node);
          entry[usage === "call" || usage === "write" ? usage : "read"] += 1;
          break;
        }
        case "MetaProperty":
          if (node.meta.name === "import") {
            const member =
              node.parent?.type === "MemberExpression" && node.parent.object === node ? node.parent : undefined;
            this.#count(this.importMeta, (member && staticKey(member)) ?? "(the object itself)", file);
          }
          break;
        case "ImportExpression":
          this.dynamicImports += 1;
          this.#specifier(node.source, file);
          break;
        case "CallExpression":
          if (node.callee.type === "Identifier" && node.callee.name === "require" && node.arguments.length === 1) {
            this.#specifier(node.arguments[0], file);
          }
          break;
        default:
          if (node.source) this.#specifier(node.source, file);
      }
    }
  }

  #specifier(node, file) {
    if (node.type !== "Literal" || typeof node.value !== "string") return;
    const host = hostModule(node.value);
    if (host) this.#count(this.hostModules, host.name, file);
    else this.otherSpecifiers.add(node.value);
  }

  toJSON() {
    const sorted = (map, value) =>
      Object.fromEntries([...map].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([k, v]) => [k, value(v)]));
    return {
      node: process.version,
      globalsPackage: GLOBALS_VERSION,
      files: this.files,
      bytes: this.bytes,
      globals: sorted(this.globals, (entry) => ({
        files: entry.files.size,
        usage: entry.usage,
        paths: sorted(entry.paths, (usage) => usage),
      })),
      memberNames: sorted(this.memberNames, (entry) => ({ ...entry, files: entry.files.size })),
      hostModules: sorted(this.hostModules, (files) => files.size),
      otherSpecifiers: this.otherSpecifiers.size,
      importMeta: sorted(this.importMeta, (files) => files.size),
      dynamicImports: this.dynamicImports,
    };
  }
}

function sourceFiles(root) {
  if (root.startsWith("@"))
    return readFileSync(root.slice(1), "utf8")
      .split("\n")
      .filter((line) => line !== "");
  if (statSync(root).isFile()) return [root];
  return readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && SOURCE_EXTENSIONS.has(path.extname(entry.name)))
    .map((entry) => path.join(entry.parentPath, entry.name))
    .sort();
}

/** The inventory of the files at `paths`, as the JSON value `render` reads. Throws on a file that does not parse. */
export function scan(paths) {
  const inventory = new Inventory();
  for (const file of paths.flatMap(sourceFiles)) {
    try {
      inventory.addFile(file, readFileSync(file, "utf8"));
    } catch (error) {
      throw new Error(`${file}: ${error.message}`, { cause: error });
    }
  }
  return inventory.toJSON();
}

/** The inventory of one source text: what the tests read. */
export function scanSource(source) {
  const inventory = new Inventory();
  inventory.addFile("input.js", source);
  return inventory.toJSON();
}

// ---- the report

/** `["a", "a.b", "a.c", "d"]` under `root` -> `root.a.{b,c}` and `root.d`: one entry per parent, a path that another extends left out. */
function groupPaths(root, paths) {
  const groups = new Map();
  for (const key of paths) {
    if (paths.some((other) => other.startsWith(`${key}.`))) continue;
    const members = key.split(".");
    const parent = [root, ...members.slice(0, -1)].join(".");
    if (!groups.has(parent)) groups.set(parent, []);
    groups.get(parent).push(members.at(-1));
  }
  return [...groups].map(([parent, names]) =>
    names.length === 1 ? `\`${parent}.${names[0]}\`` : `\`${parent}.{${names.join(",")}}\``
  );
}

const code = (names) => names.map((name) => `\`${name}\``).join(", ");

/**
 * Markdown for an inventory. Without `full`, only names a public list has: see the head of this file.
 * A member path is split at the first member this Node's objects do not have.
 */
export function render(inventory, { full = false } = {}) {
  const owners = standardMemberOwners();
  const lines = [];
  const section = (title, body) => {
    if (body.some((line) => line !== "")) lines.push(`## ${title}`, "", ...body, "");
  };
  const byEnvironment = { ecmascript: [], web: [], node: [], bun: [], otherHosts: [], unlisted: [] };
  for (const name of Object.keys(inventory.globals)) {
    const environments = environmentsOf(name);
    byEnvironment[environments[0] ?? "unlisted"].push(name);
  }
  const split = (name) => {
    const resolved = [];
    const unresolved = [];
    for (const key of Object.keys(inventory.globals[name].paths)) {
      const members = key.split(".");
      (resolvedMembers(name, members) === members.length ? resolved : unresolved).push(key);
    }
    return { resolved, unresolved };
  };
  const written = [];
  let unresolvedPaths = 0;
  const members = (names) =>
    names.flatMap((name) => {
      const { resolved, unresolved } = split(name);
      unresolvedPaths += unresolved.length;
      const entry = inventory.globals[name];
      if (entry.usage.write > 0) written.push(name);
      for (const key of full ? [...resolved, ...unresolved] : resolved) {
        if (entry.paths[key].write > 0) written.push(`${name}.${key}`);
      }
      return groupPaths(name, full ? [...resolved, ...unresolved] : resolved);
    });

  const fileCount = inventory.files.module + inventory.files.script;
  lines.push(
    "# Standard and host features in use",
    "",
    `Generated by \`host_inventory.mjs render${full ? " --full" : ""}\` (then \`prettier --write\`) from a scan of ${fileCount} files ` +
      `(${inventory.files.module} parse as modules, ${inventory.files.script} only as scripts; ${(inventory.bytes / 1e6).toFixed(1)} MB). ` +
      `Globals are classified by \`globals\` ${inventory.globalsPackage}; member paths are checked against Node ${inventory.node}.`,
    "",
    "A global is a name that no declaration in its file binds. A member path starts at a global and follows the names the source spells, " +
      "three at most, through `window`, `self`, `globalThis` and `global` and through an object pattern. " +
      "The scan does not see a global reached through a local alias or a computed key, a member reached through a value " +
      "(the two sections on members of standard objects bound those by name), or code built at run time. A name here is used somewhere; a missing name is not proved unused. " +
      "The scan reads all code, also function bodies that never run while a module is evaluated: a name here is not a claim that it is used at start-up.",
    ""
  );
  if (!full) {
    lines.push(
      "Only names that are public already are printed: a global that a list of `globals` has, a member that Node's own objects have, " +
        "a host module, a key of `import.meta`. The last section counts the rest.",
      ""
    );
  }
  section("ECMAScript globals", [code(byEnvironment.ecmascript)]);
  section(
    "Members of ECMAScript globals, by a static path",
    members(byEnvironment.ecmascript).map((line) => `- ${line}`)
  );
  const web = members(byEnvironment.web);
  const node = members(byEnvironment.node);
  const bun = members(byEnvironment.bun);
  const otherHosts = members(byEnvironment.otherHosts);
  section("Web platform globals", [code(byEnvironment.web)]);
  section("Node.js globals that the web platform does not have", [code(byEnvironment.node)]);
  section("Bun globals that neither has", [code(byEnvironment.bun)]);
  section("Globals of other hosts (Deno, browser extensions, Windows Script Host, AMD loaders)", [
    code(byEnvironment.otherHosts),
  ]);
  section(
    full ? "Members of host globals, by a static path" : "Members of host globals that Node has too, by a static path",
    [...web, ...node, ...bun, ...otherHosts].map((line) => `- ${line}`)
  );
  if (full) {
    section("Globals no list has", [code(byEnvironment.unlisted)]);
    section(
      "Members of globals no list has",
      members(byEnvironment.unlisted).map((line) => `- ${line}`)
    );
  } else {
    members(byEnvironment.unlisted);
  }
  section("Assigned, updated or deleted", [
    code(written.filter((name) => full || environmentsOf(name.split(".")[0]).length > 0)),
  ]);

  const named = Object.keys(inventory.memberNames);
  const reached = new Set(named);
  for (const [name, entry] of Object.entries(inventory.globals)) {
    for (const key of Object.keys(entry.paths)) reached.add(key.split(".").at(-1));
    reached.add(name);
  }
  const neverNamed = new Map(); // owner -> the members no file names
  for (const [member, memberOwners] of owners) {
    if (reached.has(member)) continue;
    for (const owner of memberOwners) {
      if (!neverNamed.has(owner)) neverNamed.set(owner, []);
      neverNamed.get(owner).push(member);
    }
  }
  section("Members of standard objects named on another receiver", [
    `${named.length} of the ${owners.size} names that a standard object of this Node has as a member occur as a static property ` +
      "of a value that is not a path from a global. The receiver is unknown, so each is a possible use.",
    "",
    code(named),
  ]);
  section(
    "Members of standard objects that no file names",
    [...neverNamed]
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([owner, names]) => `- \`${owner}\`: ${code(names.sort())}`)
  );
  section("Host modules", [code(Object.keys(inventory.hostModules))]);
  section("Keys of `import.meta`", [code(Object.keys(inventory.importMeta))]);
  section("Not printed", [
    full
      ? `${inventory.otherSpecifiers} other module specifiers; ${inventory.dynamicImports} \`import()\` expressions.`
      : `${byEnvironment.unlisted.length} globals that no list has, ${unresolvedPaths} member paths that do not resolve on Node's objects, ` +
        `${inventory.otherSpecifiers} other module specifiers; ${inventory.dynamicImports} \`import()\` expressions.`,
  ]);
  return `${lines.join("\n").trimEnd()}\n`;
}

function main(argv) {
  const [command, ...rest] = argv;
  if (command === "scan" && rest.length > 0) {
    process.stdout.write(`${JSON.stringify(scan(rest), null, 1)}\n`);
  } else if (command === "render" && rest.filter((arg) => arg !== "--full").length === 1) {
    const file = rest.find((arg) => arg !== "--full");
    process.stdout.write(render(JSON.parse(readFileSync(file, "utf8")), { full: rest.includes("--full") }));
  } else {
    console.error("usage: host_inventory.mjs scan PATH... | render [--full] inventory.json");
    // read by a shell: the caller's `set -e`
    process.exitCode = 2;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) main(process.argv.slice(2));
