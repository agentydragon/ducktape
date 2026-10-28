import assert from "node:assert/strict";
import { execFile } from "node:child_process";
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

const converter = join(
  RUNFILES_DIRECTORY,
  RUNFILES_WORKSPACE,
  "devinfra",
  "js",
  "openapi_typescript_codegen_",
  "openapi_typescript_codegen"
);
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
const tempDirectory = await mkdtemp(join(tmpdir(), "openapi-typescript-no-fetch-"));

async function run(input, output) {
  return await execFileAsync(converter, [input, output], {
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

  await assert.rejects(() => run(remoteOpenApiPath, join(tempDirectory, "remote.openapi.d.ts")));
  assert.equal(requests, 0, "openapi-typescript made an HTTP request for a remote $ref");

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
  await run(localOpenApiPath, join(tempDirectory, "local.openapi.d.ts"));
  assert.equal(requests, 0, "an absolute $id on a local OpenAPI file triggered an HTTP request");
} finally {
  await new Promise((resolve) => server.close(resolve));
  await rm(tempDirectory, { recursive: true, force: true });
}
