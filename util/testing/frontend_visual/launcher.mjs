/**
 * The one Puppeteer launcher for JS visual/screenshot tests, mirroring the
 * Python side (util/testing/frontend_visual.py, Playwright): both read the
 * same chromium-flags.json and frozen-clock.js, and both resolve the hermetic
 * browser from CHROMIUM_HEADLESS_SHELL (the Bazel-wired
 * @playwright_browsers//:chromium-headless-shell rootpath), falling back to
 * the ambient PLAYWRIGHT_BROWSERS_PATH for a local `bazel run`.
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const __dirname = dirname(fileURLToPath(import.meta.url));

// The shared assets live one directory up: a data file under
// util/testing/frontend_visual/ would turn that directory into a namespace
// package shadowing the sibling frontend_visual.py module for mypy.
const FLAGS = JSON.parse(readFileSync(join(__dirname, "..", "chromium-flags.json"), "utf8"));
export const CONTAINER_BASE_ARGS = FLAGS.containerBase;
export const DETERMINISTIC_EXTRA_ARGS = FLAGS.deterministicExtra;

/** Init-script text that freezes the page's wall clock at `nowMs`. */
export function frozenClockScript(nowMs) {
  const source = readFileSync(join(__dirname, "..", "frozen-clock.js"), "utf8");
  return `(() => { ${source}\nfrozenClock(${nowMs}); })();`;
}

/** Launch headless Chromium with the container-safe base flags plus `args`. */
export async function launchBrowser({ args = [], headless = true, userDataDir } = {}) {
  const launchOptions = { args: [...CONTAINER_BASE_ARGS, ...args], headless };
  if (userDataDir) {
    launchOptions.userDataDir = userDataDir;
  }
  const executablePath = resolveChromiumExecutable();
  if (executablePath) {
    launchOptions.executablePath = executablePath;
  }
  return puppeteer.launch(launchOptions);
}

/** `launchBrowser` with the deterministic-rendering flag set on top. */
export async function launchDeterministicBrowser({ args = [], userDataDir } = {}) {
  return launchBrowser({ args: [...DETERMINISTIC_EXTRA_ARGS, ...args], userDataDir });
}

export function resolveChromiumExecutable() {
  const root =
    process.env.CHROMIUM_HEADLESS_SHELL ||
    (process.env.PLAYWRIGHT_BROWSERS_PATH ? join(process.env.PLAYWRIGHT_BROWSERS_PATH, "chromium") : null);
  if (!root) {
    return null;
  }
  const resolved = resolve(root);
  const headlessShell = join(resolved, "chrome-linux", "headless_shell");
  return existsSync(headlessShell) ? headlessShell : resolved;
}
