// `kubectl-passthrough-mcp` preview screenshot entry — esbuild bundles this into the `:previews`
// IIFE: the mount of this server's fixtures.
import { mountPreviewCards } from "../screenshot/mount";

import { PREVIEW_FIXTURES } from "./preview_fixtures";

mountPreviewCards(PREVIEW_FIXTURES);
