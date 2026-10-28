import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const RUNFILES_DIRECTORY = process.env.RUNFILES_DIR ?? process.env.TEST_SRCDIR;
const RUNFILES_WORKSPACE = process.env.TEST_WORKSPACE ?? "_main";
if (RUNFILES_DIRECTORY === undefined) {
  throw new Error("Bazel did not provide a runfiles directory");
}

function converter(name) {
  const packageDirectory = join(RUNFILES_DIRECTORY, RUNFILES_WORKSPACE, "devinfra", "js");
  const candidates = [join(packageDirectory, name), join(packageDirectory, `${name}_`, name)];
  const executable = candidates.find((candidate) => existsSync(candidate));
  if (executable === undefined) {
    throw new Error(`Bazel runfiles do not contain converter ${name}: ${candidates.join(", ")}`);
  }
  return executable;
}

let requests = 0;
const server = createServer((_request, response) => {
  requests += 1;
  response.writeHead(200, { "content-type": "application/schema+json" });
  response.end(JSON.stringify({ type: "string" }));
});
await new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", resolve);
});

const address = server.address();
assert.ok(address && typeof address === "object");
const remoteSchema = `http://127.0.0.1:${address.port}/remote.json`;
const tempDirectory = await mkdtemp(join(tmpdir(), "schema-remote-references-"));

async function run(binary, args) {
  return await execFileAsync(converter(binary), args, {
    cwd: tempDirectory,
    env: { ...process.env, BAZEL_BINDIR: "." },
    timeout: 15_000,
  });
}

try {
  const remoteOpenApi = {
    openapi: "3.1.0",
    info: { title: "Remote reference test", version: "1.0.0" },
    paths: {
      "/remote": {
        get: {
          operationId: "getRemote",
          responses: {
            200: {
              description: "Remote schema",
              content: { "application/json": { schema: { $ref: remoteSchema } } },
            },
          },
        },
      },
    },
  };
  const remoteOpenApiPath = join(tempDirectory, "remote.openapi.json");
  await writeFile(remoteOpenApiPath, JSON.stringify(remoteOpenApi));

  await assert.rejects(() =>
    run("openapi_typescript_codegen", [remoteOpenApiPath, join(tempDirectory, "remote.openapi.d.ts")])
  );
  assert.equal(requests, 0, "openapi-typescript made an HTTP request for a remote $ref");

  const configPath = join(tempDirectory, "openapi-ts.config.mjs");
  await writeFile(configPath, 'export default { plugins: [{ name: "zod" }] };\n');
  await assert.rejects(() =>
    run("openapi_zod_codegen", [remoteOpenApiPath, join(tempDirectory, "remote-zod"), configPath])
  );
  assert.equal(requests, 0, "Hey API made an HTTP request for a remote $ref");

  const remoteJsonSchemaPath = join(tempDirectory, "remote.schema.json");
  await writeFile(
    remoteJsonSchemaPath,
    JSON.stringify({
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: "https://schemas.example.test/remote-root.json",
      title: "RemoteSchema",
      type: "object",
      properties: { value: { $ref: remoteSchema } },
    })
  );
  await assert.rejects(() =>
    run("json_schema_typescript_codegen", [remoteJsonSchemaPath, join(tempDirectory, "remote.schema.d.ts")])
  );
  assert.equal(requests, 0, "json-schema-to-typescript made an HTTP request for a remote $ref");

  // An absolute `$id` on a local file is metadata; it must not turn the file input into a fetch.
  const localOpenApi = {
    openapi: "3.1.0",
    info: { title: "Local file test", version: "1.0.0" },
    paths: {
      "/local": {
        get: {
          operationId: "getLocal",
          responses: {
            200: {
              description: "Local schema",
              content: { "application/json": { schema: { $ref: "#/components/schemas/LocalModel" } } },
            },
          },
        },
      },
    },
    components: {
      schemas: {
        LocalModel: {
          $id: "https://schemas.example.test/local-model.json",
          type: "object",
          properties: { name: { type: "string" } },
        },
      },
    },
  };
  const localOpenApiPath = join(tempDirectory, "local.openapi.json");
  await writeFile(localOpenApiPath, JSON.stringify(localOpenApi));
  await run("openapi_typescript_codegen", [localOpenApiPath, join(tempDirectory, "local.openapi.d.ts")]);
  await run("openapi_zod_codegen", [localOpenApiPath, join(tempDirectory, "local-zod"), configPath]);

  const localJsonSchemaPath = join(tempDirectory, "local.schema.json");
  await writeFile(
    localJsonSchemaPath,
    JSON.stringify({
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: "https://schemas.example.test/local-root.json",
      title: "LocalModel",
      type: "object",
      properties: { name: { type: "string" } },
    })
  );
  await run("json_schema_typescript_codegen", [localJsonSchemaPath, join(tempDirectory, "local.schema.d.ts")]);
  assert.equal(requests, 0, "a local file input with an absolute $id triggered an HTTP request");
} finally {
  await new Promise((resolve) => server.close(resolve));
  await rm(tempDirectory, { recursive: true, force: true });
}
