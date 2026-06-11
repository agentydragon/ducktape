import { isAbsolute, resolve } from "node:path";

export function requireValue(argv, index, flag) {
  const value = argv[index];
  if (!value || value.startsWith("--")) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

export function resolveWorkspacePath(path) {
  if (isAbsolute(path)) {
    return path;
  }
  const workspace = process.env.BUILD_WORKSPACE_DIRECTORY ?? process.env.BUILD_WORKING_DIRECTORY ?? process.env.PWD;
  if (workspace) {
    return resolve(workspace, path);
  }
  return resolve(process.cwd(), path);
}
