#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const os = require("os");
const libA = path.join(__dirname, "lib", "system-utils");
const libB = path.join(os.homedir(), ".claude-code-router", "transformers", "lib", "system-utils");
const { extractSystemBlobs } = require(fs.existsSync(libA + ".js") ? libA : libB);

function esc(x) {
  return x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function readAllStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

(async () => {
  const templatePath = process.argv[2];
  if (!templatePath) {
    console.error("usage: system_rewrite_apply.js <template-file> < input-system.txt > output-system.txt");
    process.exit(2);
  }
  const template = fs.readFileSync(templatePath, "utf8");
  const sysIn = await readAllStdin();
  const { toolsBlob, envGitBlobs, modelLine, mcpSection } = extractSystemBlobs(String(sysIn));
  const ctx = {
    toolsBlob,
    envGitBlobs: envGitBlobs.join(""),
    modelLine,
    mcpSection,
  };

  // Enforce that template only uses the supported variables at most once each
  const coreVars = ["toolsBlob", "envGitBlobs", "modelLine", "mcpSection"];
  for (const name of coreVars) {
    const reVar = new RegExp(`\\{\\{${esc(name)}\\}\\}`, "g");
    const count = (template.match(reVar) || []).length;
    if (count > 1) {
      console.error(`template variable ${name} appears ${count} times (expected \u22641)`);
      process.exit(6);
    }
  }
  // Reject any other {{...}} tokens (including sections like {{#...}} or {{/...}})
  const tokenRe = /\{\{\s*([#\/]?\w+)\s*}}/g;
  let m;
  while ((m = tokenRe.exec(template)) !== null) {
    const raw = m[1];
    if (!coreVars.includes(raw)) {
      console.error(`template contains unsupported token '{{${raw}}}'`);
      process.exit(5);
    }
  }

  // Perform exact replacements for the known variables only
  let out = template
    .replace("{{toolsBlob}}", ctx.toolsBlob)
    .replace("{{envGitBlobs}}", ctx.envGitBlobs)
    .replace("{{modelLine}}", ctx.modelLine)
    .replace("{{mcpSection}}", ctx.mcpSection);

  // Validate no unreplaced tokens remain
  if (/\{\{\s*\w+\s*}}/.test(out)) {
    const leftover = out.match(/\{\{\s*\w+\s*}}/);
    console.error(`template contains unreplaced tokens, e.g. '${leftover[0]}'`);
    process.exit(4);
  }

  process.stdout.write(out);
})();
