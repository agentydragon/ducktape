// `gmail` preview screenshot entry — esbuild bundles this into the `:previews` IIFE: the
// Gmail-only fetch stub, imported before the registry/widget graph reaches client.ts, then the
// mount of this server's fixtures.
import "./preview_mock";

import { mountPreviewCards } from "../screenshot/mount";

import { PREVIEW_FIXTURES } from "./preview_fixtures";

mountPreviewCards(PREVIEW_FIXTURES);
