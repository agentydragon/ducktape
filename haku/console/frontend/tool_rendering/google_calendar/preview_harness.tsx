// `google_calendar` preview screenshot entry — esbuild bundles this into the `:previews` IIFE and
// mounts this server's fixtures.

import { mountPreviewCards } from "../screenshot/mount";

import { PREVIEW_FIXTURES } from "./preview_fixtures";

mountPreviewCards(PREVIEW_FIXTURES);
