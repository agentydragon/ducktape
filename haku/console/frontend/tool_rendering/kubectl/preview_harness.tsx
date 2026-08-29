// `kubectl-passthrough-mcp` preview screenshot entry — esbuild bundles this into the `:previews`
// IIFE. Holds the fixtures plus the mount call; `mount` is imported FIRST so its fetch stub is
// installed before the registry/widget graph reaches client.ts. `satisfies
// RegisteredToolPreviewFixture` ties each (serverId, toolName, args) to the registry's real Zod
// schemas, so a stale id or argument is a type error.
import { mountPreviewCards } from "../screenshot/mount";

import type { RegisteredToolPreviewFixture } from "../index";

const PREVIEW_FIXTURES = [
  {
    title: "Deploy the worker service",
    serverId: "kubectl-passthrough-mcp",
    toolName: "resources_create_or_update",
    args: {
      resource:
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: worker\n  namespace: haku-sandbox\nspec:\n  replicas: 3\n  selector:\n    matchLabels:\n      app: worker\n  template:\n    metadata:\n      labels:\n        app: worker\n    spec:\n      containers:\n        - name: worker\n          image: ghcr.io/agentydragon/worker:latest",
    },
  },
  {
    title: "Delete the failed worker pod",
    serverId: "kubectl-passthrough-mcp",
    toolName: "resources_delete",
    args: { apiVersion: "v1", kind: "Pod", name: "worker-6f9c2", namespace: "haku-sandbox", gracePeriodSeconds: 0 },
  },
  {
    title: "Inspect the worker deployment",
    serverId: "kubectl-passthrough-mcp",
    toolName: "resources_get",
    args: { apiVersion: "apps/v1", kind: "Deployment", name: "worker", namespace: "haku-sandbox" },
  },
  {
    title: "Restart the failed worker pod",
    serverId: "kubectl-passthrough-mcp",
    toolName: "pods_delete",
    args: { name: "worker-6f9c2", namespace: "haku-sandbox" },
  },
  {
    title: "List running worker pods",
    serverId: "kubectl-passthrough-mcp",
    toolName: "pods_list_in_namespace",
    args: {
      namespace: "haku-sandbox",
      labelSelector: "app=worker",
      fieldSelector: "status.phase=Running",
    },
  },
  {
    title: "Check the worker health endpoint",
    serverId: "kubectl-passthrough-mcp",
    toolName: "pods_exec",
    args: {
      name: "worker-6f9c2",
      namespace: "haku-sandbox",
      container: "worker",
      command: ["sh", "-c", "curl -fsS http://127.0.0.1:8080/healthz"],
    },
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
