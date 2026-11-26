import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { prepare, root } from "./harness.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";

const entry = "entry.mjs";

function invokeManifest(manifest, out, args = [], env = process.env) {
  const result = spawnSync(process.env.LOWERSPLIT, [manifest, out, ...args], {
    encoding: "utf8",
    env,
  });
  assert.ifError(result.error);
  assert(result.stdout, result.stderr);
  const verdict = JSON.parse(result.stdout.trim());
  assert.equal(result.status, verdict.status === "certified" ? 0 : 1, result.stdout + result.stderr);
  return { result, verdict };
}

function invoke(files, out, args = [], env = process.env) {
  return invokeManifest(writeDeployment(files.sourceFile, files.specFile, out), out, args, env);
}

function refuseFresh(files, name, args, requested = "all-outcomes", env = process.env) {
  const out = path.join(files.dir, name);
  const { result, verdict } = invoke(files, out, args, env);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.equal(verdict.status, "rejected", result.stdout + result.stderr);
  assert.equal(verdict.requested, requested, result.stdout + result.stderr);
  assert.equal(verdict.guarantee, null, result.stdout + result.stderr);
  assert(!existsSync(out), `refusal created ${out}`);
  return { out, verdict };
}

function refuseFreshManifest(manifest, out, args, requested = "all-outcomes") {
  const { result, verdict } = invokeManifest(manifest, out, args);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.equal(verdict.status, "rejected", result.stdout + result.stderr);
  assert.equal(verdict.requested, requested, result.stdout + result.stderr);
  assert.equal(verdict.guarantee, null, result.stdout + result.stderr);
  assert(!existsSync(out), `refusal created ${out}`);
}

function snapshotFiles(directory, prefix = "") {
  return Object.fromEntries(
    readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => left.name.localeCompare(right.name))
      .flatMap((entry) => {
        const relative = path.join(prefix, entry.name);
        const full = path.join(directory, entry.name);
        if (entry.isDirectory()) return Object.entries(snapshotFiles(full, relative));
        assert(entry.isFile(), `unexpected output entry ${full}`);
        return [[relative, readFileSync(full)]];
      })
  );
}

function diagnosticPreview(files, out) {
  const manifest = writeDeployment(files.sourceFile, files.specFile, out);
  const result = spawnSync(process.env.SPLIT_PREVIEW, [manifest, out, "--force"], {
    encoding: "utf8",
  });
  assert.ifError(result.error);
  assert([0, 1].includes(result.status), result.stdout + result.stderr);
  assert.match(result.stdout, /model diagnostic: /, result.stdout + result.stderr);
  assert(existsSync(out), `diagnostic preview was not written to ${out}`);
  return result.stdout;
}

function observeAbrupt(file) {
  const script = `
    try {
      await import(process.env.CONSTRUCTION_CUT_MODULE);
      console.log(JSON.stringify({ completion: "normal" }));
    } catch (error) {
      console.log(JSON.stringify({ completion: "throw", value: error }));
    }
  `;
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
    encoding: "utf8",
    env: { ...process.env, CONSTRUCTION_CUT_MODULE: pathToFileURL(file).href },
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  return JSON.parse(result.stdout.trim());
}

// This is the formerly successful two-module browser deployment from
// provider_location_test.mjs. The public construction-only cut must refuse it
// whether the caller asks for the strict default or tries to select the weaker
// normal-completion guarantee through --require.
{
  const provider = "nested/provider.mjs";
  const lazy = "nested/lazy.mjs";
  const worker = "renderer.mjs";
  const dir = path.join(root, "construction-cut-former-deployment");
  mkdirSync(path.join(dir, "nested"), { recursive: true });
  const files = prepare(dir, {
    sourceName: entry,
    source: `import { load, meta, update } from "./${provider}";
      var first = 0; var render = () => 42; var entryMeta = () => import.meta;
      export { load, meta, update, render, entryMeta };`,
    provider: "",
    spec: {
      entry,
      modules: { [entry]: ["#0", "#1", "#3", "#4"], [worker]: ["#2"] },
      order: [worker],
      sequential: true,
    },
    extras: {
      [provider]: `export var current = "before";
        export var update = value => { current = value; };
        export var meta = () => import.meta;
        export var load = () => import("./lazy.mjs");`,
      [lazy]: 'import { current } from "./provider.mjs"; import "./cycle.mjs"; export const sample = () => current;',
      "nested/cycle.mjs": 'import "./lazy.mjs";',
      "nested/lazy.mjs": 'export const sample = () => "wrong referrer";',
    },
  });
  files.dir = path.dirname(files.sourceFile);
  const host = path.join(files.dir, "host.json");
  writeFileSync(host, JSON.stringify({ profile: "browser-page" }));
  const formerOptions = [
    "--structural",
    "--deployment-origin",
    "https://example.org",
    "--host-contract",
    host,
    "--deployment-preset",
    "native-browser-record@1",
  ];

  refuseFresh(files, "fresh-default", formerOptions);
  const normalRefusal = refuseFresh(files, "fresh-normal", [...formerOptions, "--require", "normal-completion"]);
  refuseFresh(files, "fresh-all", [...formerOptions, "--require", "all-outcomes"]);

  // The automatic selector and the strategy-census wrapper must not recover a
  // weaker certificate by dropping --structural or trying its alternate path.
  refuseFresh(files, "fresh-automatic", ["--require", "normal-completion"]);
  const censusOut = path.join(files.dir, "fresh-census");
  const censusEnv = {
    ...process.env,
    SPLIT_PROOF_STRATEGY_CENSUS: "1",
  };
  const census = invoke(files, censusOut, [...formerOptions, "--require", "normal-completion"], censusEnv);
  assert.equal(census.result.status, 1, census.result.stdout + census.result.stderr);
  assert.equal(census.verdict.status, "rejected", census.result.stdout + census.result.stderr);
  assert.equal(census.verdict.requested, "all-outcomes", census.result.stdout + census.result.stderr);
  assert.equal(census.verdict.guarantee, null, census.result.stdout + census.result.stderr);
  assert(!existsSync(censusOut), "census refusal created a fresh destination");
  assert.equal(census.verdict.status, normalRefusal.verdict.status);
  assert.equal(census.verdict.reason, normalRefusal.verdict.reason);

  // A test-only preview is deliberately not a certificate. Check-output must
  // refuse it without changing any existing module bytes.
  const existing = path.join(files.dir, "existing-preview");
  diagnosticPreview(files, existing);
  const before = snapshotFiles(existing);
  const checked = invoke(files, existing, [...formerOptions, "--check-output"]);
  assert.equal(checked.result.status, 1, checked.result.stdout + checked.result.stderr);
  assert.equal(checked.verdict.status, "rejected", checked.result.stdout + checked.result.stderr);
  assert.equal(checked.verdict.guarantee, null, checked.result.stdout + checked.result.stderr);
  assert.deepEqual(snapshotFiles(existing), before, "check-output changed bytes in the existing directory");
}

// The existing joint_static_test three-provider fixture is a successful
// structural deployment with four input chunks and --deployment-origin. Keep
// this true multi-chunk shape alongside the provider_location single-chunk
// deployment above. A check of an already populated destination must also be
// read-only when the public construction-only path refuses.
{
  const dir = path.join(root, "construction-cut-multiple-deployment-chunks");
  mkdirSync(dir, { recursive: true });
  const app = "app.mjs";
  const providers = ["first", "second", "third"];
  const source = providers
    .map((name) => `import { value as ${name} } from "./${name}.mjs";`)
    .concat("export const total = first + second + third;")
    .join("\n");
  writeFileSync(path.join(dir, app), source);
  const chunks = [{ path: app, source: app }];
  for (const [index, name] of providers.entries()) {
    const route = `${name}.mjs`;
    const owner = `${name}-body`;
    const providerSource = `const value = ${index + 1}; export { value };`;
    writeFileSync(path.join(dir, route), providerSource);
    chunks.push({
      path: route,
      source: route,
      placement: {
        chunk_id: name,
        source_sha256: createHash("sha256").update(providerSource).digest("hex"),
        import_order: [owner],
        units: [
          { ordinal: 0, owner },
          { ordinal: 1, owner: null },
        ],
      },
    });
  }
  const manifest = path.join(dir, "deployment.json");
  writeFileSync(manifest, JSON.stringify({ entry: app, chunks }));
  const options = ["--structural", "--deployment-origin", "https://deployment.test"];
  const fresh = path.join(dir, "fresh");
  refuseFreshManifest(manifest, fresh, options);

  const existing = path.join(dir, "existing");
  mkdirSync(existing);
  writeFileSync(path.join(existing, "sentinel.bin"), Buffer.from([0, 1, 2, 255]));
  const before = snapshotFiles(existing);
  const checked = invokeManifest(manifest, existing, [...options, "--check-output"]);
  assert.equal(checked.result.status, 1, checked.result.stdout + checked.result.stderr);
  assert.equal(checked.verdict.status, "rejected", checked.result.stdout + checked.result.stderr);
  assert.deepEqual(snapshotFiles(existing), before, "multi-chunk check-output changed existing bytes");
}

// Reversing two throwing initializers changes observable abrupt behavior: the
// first thrown value changes. No public guarantee request may certify this cut.
{
  const files = prepare(path.join(root, "construction-cut-abrupt-order"), {
    source: `const first = (() => { throw "first"; })();
      const second = (() => { throw "second"; })();
      export { first, second };`,
    provider: "",
    spec: {
      entry,
      modules: { "first.mjs": ["#0"], "second.mjs": ["#1"], [entry]: ["#2"] },
      order: ["second.mjs", "first.mjs"],
      sequential: true,
    },
  });
  files.dir = path.dirname(files.sourceFile);
  refuseFresh(files, "normal-completion", ["--structural", "--require", "normal-completion"]);
  refuseFresh(files, "all-outcomes", ["--structural", "--require", "all-outcomes"], "all-outcomes");

  const preview = path.join(files.dir, "unverified-preview");
  diagnosticPreview(files, preview);
  const sourceOutcome = observeAbrupt(files.sourceFile);
  const splitOutcome = observeAbrupt(path.join(preview, entry));
  assert.deepEqual(sourceOutcome, { completion: "throw", value: "first" });
  assert.deepEqual(splitOutcome, { completion: "throw", value: "second" });
  assert.notDeepEqual(sourceOutcome, splitOutcome);
}
