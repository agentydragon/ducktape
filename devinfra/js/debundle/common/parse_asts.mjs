import { parse } from "@babel/parser";
import { cloneDefaultParserOptions, DEFAULT_PARSER_OPTIONS } from "./parser_options.mjs";
import { listJsArtifactFiles, requirePipelineArtifact } from "./artifact.mjs";

export function computeJsAsts({ artifact, dropContent = true }) {
  requirePipelineArtifact(artifact, "computeJsAsts");

  let parsed = 0;
  for (const file of listJsArtifactFiles(artifact)) {
    if (file.ast) {
      continue;
    }
    if (typeof file.content !== "string") {
      throw new Error(`computeJsAsts requires content for file: ${file.path}`);
    }
    const parserOptions = file.parserOptions ?? DEFAULT_PARSER_OPTIONS;
    file.ast = parse(file.content, parserOptions);
    file.parserOptions = cloneParserOptions(parserOptions);
    if (dropContent) {
      delete file.content;
    }
    parsed++;
  }

  return {
    artifact,
    manifest: {
      kind: "js.compute_js_asts_manifest",
      counts: {
        parsed,
        files: listJsArtifactFiles(artifact).length,
      },
    },
  };
}

function cloneParserOptions(parserOptions) {
  if (parserOptions === DEFAULT_PARSER_OPTIONS) {
    return cloneDefaultParserOptions();
  }
  if (!parserOptions || typeof parserOptions !== "object") {
    return cloneDefaultParserOptions();
  }
  return {
    ...parserOptions,
    ...(Array.isArray(parserOptions.plugins) ? { plugins: [...parserOptions.plugins] } : {}),
  };
}
