import assert from "node:assert/strict";
import { createServer } from "node:net";

import { launchPuppeteerBrowser } from "../../../../util/testing/frontend_visual/puppeteer-lib.mjs";
import { parseLiveProxyArgs, startLiveProxy } from "./proxy.mjs";

async function main() {
  const options = parseBrowserLoadTestArgs(process.argv.slice(2));
  const proxyPort = await allocatePort();
  const assetPort = await allocatePort();
  const handles = await startLiveProxy({
    ...options.liveProxyOptions,
    assetPort,
    proxyPort,
  });

  const browser = await launchPuppeteerBrowser({
    args: [
      "--disable-dev-shm-usage",
      "--disable-setuid-sandbox",
      "--ignore-certificate-errors",
      "--no-sandbox",
      `--proxy-server=http://${handles.config.proxyHost}:${handles.config.proxyPort}`,
    ],
    headless: true,
  });

  const page = await browser.newPage();
  const consoleMessages = [];
  const failedRequests = [];
  const pageErrors = [];
  const responses = [];

  page.on("console", (message) => {
    const rendered = `[${message.type()}] ${message.text()}`;
    consoleMessages.push(rendered);
  });
  page.on("pageerror", (error) => pageErrors.push(error.stack ?? error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push({
      errorText: request.failure()?.errorText ?? "unknown",
      url: request.url(),
    });
  });
  page.on("response", (response) => {
    responses.push({
      status: response.status(),
      url: response.url(),
    });
  });

  try {
    await page.goto(handles.config.targetUrl, {
      timeout: options.gotoTimeoutMs,
      waitUntil: "domcontentloaded",
    });
    await page.waitForFunction(() => globalThis.__jsDebundleLiveProxy?.active === true, {
      timeout: options.waitTimeoutMs,
    });
    if (options.waitForSelector) {
      // Race the selector wait against a page-error sentinel so the
      // smoke surfaces a script load failure within seconds rather
      // than after the full selector-wait timeout. The diagnostics
      // bundle in the catch block still reports every collected
      // signal regardless of which side of the race fires.
      const errorSentinel = startPageErrorSentinel(pageErrors);
      try {
        await Promise.race([
          page.waitForSelector(options.waitForSelector, {
            timeout: options.waitTimeoutMs,
          }),
          errorSentinel.promise,
        ]);
      } finally {
        errorSentinel.cancel();
      }
    }
    await delay(1000);

    const liveProxyState = await page.evaluate(() => ({
      active: globalThis.__jsDebundleLiveProxy?.active === true,
      internalMarker: document.documentElement.dataset.jsDebundleLiveProxy,
      title: document.title,
    }));
    const bootstrapUrl = `${handles.config.targetOrigin}${handles.config.bootstrapUrl}`;
    const staticPrefix = `${handles.config.targetOrigin}${handles.config.internalPrefix}/app/static/`;

    assert.equal(liveProxyState.active, true);
    assert.equal(liveProxyState.internalMarker, "true");
    assert.ok(
      responses.some((response) => response.url === bootstrapUrl && response.status === 200),
      `missing bootstrap response for ${bootstrapUrl}\nresponses:\n${formatJson(responses)}`
    );
    assert.ok(
      responses.some((response) => response.url.startsWith(staticPrefix) && response.status === 200),
      `missing transformed static asset response under ${staticPrefix}\nresponses:\n${formatJson(responses)}`
    );
    assert.deepEqual(pageErrors, [], `page errors:\n${pageErrors.join("\n")}\nconsole:\n${consoleMessages.join("\n")}`);
    const consoleErrors = consoleMessages.filter((message) => message.startsWith("[error] "));
    assert.deepEqual(
      consoleErrors,
      [],
      `console errors:\n${consoleErrors.join("\n")}\nfailed requests:\n${formatJson(failedRequests)}`
    );
    assert.deepEqual(
      failedRequests.filter((request) =>
        request.url.startsWith(`${handles.config.targetOrigin}${handles.config.internalPrefix}/`)
      ),
      [],
      `failed internal asset requests:\n${formatJson(failedRequests)}\nconsole:\n${consoleMessages.join("\n")}`
    );
  } catch (error) {
    const diagnostics = await collectFailureDiagnostics(page, {
      consoleMessages,
      failedRequests,
      pageErrors,
      responses,
    });
    error.message = `${error.message}\n\n${diagnostics}`;
    throw error;
  } finally {
    await browser.close();
    await handles.close();
  }
}

function parseBrowserLoadTestArgs(argv) {
  const liveProxyArgv = [];
  let waitForSelector = null;
  let waitTimeoutMs = 30_000;
  let gotoTimeoutMs = 30_000;

  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    switch (arg) {
      case "--wait-for-selector":
        waitForSelector = requireValue(argv[++index], arg);
        break;
      case "--wait-timeout-ms":
        waitTimeoutMs = parsePositiveInteger(requireValue(argv[++index], arg), arg);
        break;
      case "--goto-timeout-ms":
        gotoTimeoutMs = parsePositiveInteger(requireValue(argv[++index], arg), arg);
        break;
      default:
        liveProxyArgv.push(arg);
        break;
    }
  }

  return {
    gotoTimeoutMs,
    liveProxyOptions: parseLiveProxyArgs(liveProxyArgv),
    waitForSelector,
    waitTimeoutMs,
  };
}

function requireValue(value, flag) {
  if (value === undefined) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

function parsePositiveInteger(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`${flag} must be a positive integer, got ${value}`);
  }
  return parsed;
}

function allocatePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = address?.port;
      server.close((error) => {
        if (error) {
          reject(error);
          return;
        }
        resolve(port);
      });
    });
  });
}

function delay(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function startPageErrorSentinel(pageErrors) {
  // Polls the shared `pageErrors` buffer (filled by the puppeteer
  // `pageerror` listener) and rejects as soon as anything lands.
  // The caller `cancel()`s once the race resolves so the interval
  // doesn't leak past the test.
  let interval = null;
  const promise = new Promise((_, reject) => {
    interval = setInterval(() => {
      if (pageErrors.length > 0) {
        clearInterval(interval);
        interval = null;
        reject(new Error(`page errored before selector resolved:\n${pageErrors.join("\n")}`));
      }
    }, 200);
  });
  return {
    promise,
    cancel() {
      if (interval !== null) {
        clearInterval(interval);
        interval = null;
      }
    },
  };
}

function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

async function collectFailureDiagnostics(page, { consoleMessages, failedRequests, pageErrors, responses }) {
  let pageState = null;
  try {
    pageState = await page.evaluate(() => ({
      bodyText: document.body?.innerText?.slice(0, 2000) ?? "",
      documentReadyState: document.readyState,
      html: document.documentElement?.outerHTML?.slice(0, 4000) ?? "",
      liveProxyActive: globalThis.__jsDebundleLiveProxy?.active === true,
      liveProxyMarker: document.documentElement.dataset.jsDebundleLiveProxy ?? null,
      location: location.href,
      title: document.title,
    }));
  } catch (error) {
    pageState = {
      evaluateError: error.stack ?? error.message,
    };
  }
  const recentResponses = responses.slice(-50);
  return [
    "browser diagnostics:",
    `page errors:\n${pageErrors.join("\n") || "<none>"}`,
    `console:\n${consoleMessages.join("\n") || "<none>"}`,
    `failed requests:\n${formatJson(failedRequests)}`,
    `recent responses:\n${formatJson(recentResponses)}`,
    `page state:\n${formatJson(pageState)}`,
  ].join("\n");
}

await main();
