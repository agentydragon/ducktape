import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import puppeteer from "puppeteer";

export async function launchPuppeteerBrowser({ args = [], headless = true } = {}) {
  const launchOptions = {
    args,
    headless,
  };

  const executablePath = resolvePuppeteerExecutable(process.env.PUPPETEER_EXECUTABLE_PATH);
  if (executablePath) {
    launchOptions.executablePath = executablePath;
  }
  return puppeteer.launch(launchOptions);
}

export function resolvePuppeteerExecutable(value) {
  if (!value) {
    return null;
  }
  const resolved = resolve(value);
  const headlessShell = join(resolved, "chrome-linux", "headless_shell");
  if (existsSync(headlessShell)) {
    return headlessShell;
  }
  return resolved;
}
