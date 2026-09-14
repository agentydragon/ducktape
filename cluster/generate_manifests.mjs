import { mkdirSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

import { ApiObject, App, Chart } from "cdk8s";

const outputFlag = "--output-dir";
const outputFlagIndex = process.argv.indexOf(outputFlag);
const outputArgument = outputFlagIndex === -1 ? undefined : process.argv[outputFlagIndex + 1];

if (!outputArgument || outputArgument.startsWith("--")) {
  throw new Error(`usage: generate_manifests.mjs ${outputFlag} <directory>`);
}

const outputDirectory = isAbsolute(outputArgument) ? outputArgument : resolve(process.cwd(), outputArgument);
mkdirSync(outputDirectory, { recursive: true });

const app = new App({ outdir: outputDirectory });
const chart = new Chart(app, "cdk8s-bazel-smoke", {
  disableResourceNameHashes: true,
});

new ApiObject(chart, "generated-config", {
  apiVersion: "v1",
  kind: "ConfigMap",
  metadata: {
    name: "cdk8s-bazel-smoke",
    labels: {
      "app.kubernetes.io/name": "cdk8s-bazel-smoke",
      "app.kubernetes.io/managed-by": "cdk8s",
    },
  },
  data: {
    generated: "by cdk8s under Bazel",
    delivery: "Flux can consume this ordinary Kubernetes YAML",
  },
});

app.synth();
