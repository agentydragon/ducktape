import type { ActionPresentation } from "../presentation";
import { execPresentation } from "./exec";

export const sshRenderers: ReadonlyMap<string, ActionPresentation> = new Map([["exec", execPresentation]]);
